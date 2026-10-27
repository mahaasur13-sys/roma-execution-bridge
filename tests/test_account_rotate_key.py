"""PR-F2c: ротация API-ключа через /account.

Контракт:

  * ключ перевыпускается только из браузерной сессии и только с CSRF-токеном,
    привязанным к этой сессии (``auth.sessions.csrf_token_for``);
  * второй фактор зависит от провайдера: ``email`` — пароль (та же форма
    sha256(password + user_id), что пишет ``/auth/signup``), OAuth — сессия не
    старше 10 минут;
  * ``users.api_key_hash`` и ``tenants.api_key_hash`` меняются в ОДНОЙ транзакции;
    если в tenants не нашлось ровно одной строки — ROLLBACK, ответ 500 без деталей,
    ``users`` не тронут;
  * ``users.api_key`` после ротации пуст: рабочий креденшл в plaintext не остаётся;
  * реестр в памяти переключается ПОСЛЕ коммита: старый digest удаляется, новый
    регистрируется;
  * новый ключ показывается один раз, ответ помечен ``Cache-Control: no-store``.
"""

from __future__ import annotations

import hashlib
import re
import sys
import os
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db
import db_adapter
import deps
import main
from auth.sessions import _sessions, create_session, csrf_token_for, delete_session

NEW_KEY = re.compile(r"<code>(roma-[0-9a-f]{8}-[0-9a-f]{3})</code>")


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row(user_id: str) -> dict:
    c = db._conn()
    try:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        c.close()
    assert row is not None, f"users/{user_id} не найден"
    return dict(row)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Фоновый воркер выключен, реестр и лимитер чистые на входе и выходе."""

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)

    original = dict(deps.API_KEYS)
    deps.API_KEYS.clear()
    deps.limiter._storage.reset()
    yield
    deps.limiter._storage.reset()
    deps.API_KEYS.clear()
    deps.API_KEYS.update(original)


@pytest.fixture
def account(sqlite_tmp_db):
    """OAuth-пользователь в sqlite-ветке (пароля нет — второй фактор это свежесть сессии)."""
    adapter = sqlite_tmp_db
    db.init_db()
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
    raw = f"roma-key-{uuid.uuid4().hex[:8]}"

    adapter.seed_tenants({raw: {"tenant_id": tenant_id, "name": "rotate"}})
    adapter.upsert_oauth_user(user_id, f"{user_id}@example.com", "Test", "google", tenant_id, raw)
    deps.register_key(raw, {"tenant_id": tenant_id, "plan": "free"})

    yield {"user_id": user_id, "tenant_id": tenant_id, "raw": raw}

    c = db._conn()
    try:
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        c.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
        c.commit()
    finally:
        c.close()


@pytest.fixture
def client():
    return TestClient(main.app)


def _login(client, tenant_id: str) -> tuple[str, str]:
    session_id = create_session(tenant_id)
    client.cookies.set("session_id", session_id)
    return session_id, csrf_token_for(session_id)


def test_rotate_clears_plaintext_and_moves_both_hashes(account, client):
    """(а) одна транзакция: оба хэша = sha256(new), users.api_key обнулён."""
    session_id, token = _login(client, account["tenant_id"])

    try:
        resp = client.post("/account/rotate-key", data={"csrf_token": token})
        assert resp.status_code == 200, resp.text

        match = NEW_KEY.search(resp.text)
        assert match, f"новый ключ не показан в ответе: {resp.text[:400]}"
        new_key = match.group(1)
        assert new_key != account["raw"]

        row = _row(account["user_id"])
        assert row["api_key"] == "", "plaintext ключ обязан быть обнулён"
        assert row["api_key_hash"] == _digest(new_key)

        tenant = db.get_tenant(account["tenant_id"])
        assert tenant is not None
        assert tenant["api_key_hash"] == _digest(new_key), "tenants.api_key_hash должен переехать вместе с users"

        assert deps.lookup_key(account["raw"]) is None, "старый ключ обязан перестать работать"
        assert deps.lookup_key(new_key) is not None, "новый ключ обязан быть в реестре"
    finally:
        delete_session(session_id)


def test_rotated_response_is_not_cacheable(account, client):
    """(б) ключ отдаётся один раз — ответ не кэшируется."""
    session_id, token = _login(client, account["tenant_id"])
    try:
        resp = client.post("/account/rotate-key", data={"csrf_token": token})
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("cache-control") == "no-store"

        form = client.get("/account")
        assert form.status_code == 200, form.text
        assert form.headers.get("cache-control") == "no-store"
    finally:
        delete_session(session_id)


def test_missing_or_foreign_csrf_token_is_403(account, client):
    """(в) без CSRF-токена и с чужим токеном — 403, ключ не меняется."""
    session_id, token = _login(client, account["tenant_id"])
    other_session, other_token = _login(TestClient(main.app), account["tenant_id"])

    try:
        bare = client.post("/account/rotate-key", data={})
        assert bare.status_code == 403, bare.text

        foreign = client.post(
            "/account/rotate-key",
            data={"csrf_token": other_token if other_token != token else token + "x"},
        )
        assert foreign.status_code == 403, foreign.text

        row = _row(account["user_id"])
        assert row["api_key"] == account["raw"], "отказ CSRF не должен трогать ключ"
        assert deps.lookup_key(account["raw"]) is not None
    finally:
        delete_session(session_id)
        delete_session(other_session)


def test_no_session_is_401(client):
    """(г) без cookie-сессии роут вообще недоступен."""
    resp = client.post("/account/rotate-key", data={"csrf_token": "whatever"})
    assert resp.status_code == 401, resp.text

    page = client.get("/account", follow_redirects=False)
    assert page.status_code == 401, page.text


def test_oauth_session_older_than_ten_minutes_is_403(account, client):
    """(д) для OAuth-аккаунта старая сессия не является доказательством присутствия."""
    session_id, token = _login(client, account["tenant_id"])
    try:
        _sessions[session_id]["created_at"] = time.time() - 601

        resp = client.post("/account/rotate-key", data={"csrf_token": token})
        assert resp.status_code == 403, resp.text
        assert "sign in again" in resp.text.lower()

        row = _row(account["user_id"])
        assert row["api_key"] == account["raw"]
    finally:
        delete_session(session_id)


def test_email_account_requires_the_password(account, client, monkeypatch):
    """(е) для provider=email второй фактор — пароль; без него и с неверным — 403."""
    password = "correct-horse-battery"
    user_id = account["user_id"]
    password_hash = hashlib.sha256(password.encode() + user_id.encode()).hexdigest()
    email_user = {
        "id": user_id,
        "email": f"{user_id}@example.com",
        "provider": "email",
        "tenant_id": account["tenant_id"],
        "api_key": account["raw"],
        "password_hash": password_hash,
    }
    monkeypatch.setattr(db_adapter, "get_user_by_tenant", lambda _tid: dict(email_user))

    session_id, token = _login(client, account["tenant_id"])
    try:
        no_password = client.post(
            "/account/rotate-key",
            data={"csrf_token": token},
        )
        assert no_password.status_code == 403, no_password.text

        wrong = client.post(
            "/account/rotate-key",
            data={"csrf_token": token, "password": "wrong-password"},
        )
        assert wrong.status_code == 403, wrong.text

        ok = client.post(
            "/account/rotate-key",
            data={"csrf_token": token, "password": password},
        )
        assert ok.status_code == 200, ok.text
    finally:
        delete_session(session_id)


def test_tenant_rowcount_zero_rolls_back_users(account):
    """(ж) tenants без строки → ROLLBACK: users.api_key и хэш остаются прежними."""
    with pytest.raises(RuntimeError):
        db_adapter.rotate_user_api_key(account["user_id"], "tenant-does-not-exist", "roma-newkey1234")

    row = _row(account["user_id"])
    assert row["api_key"] == account["raw"], "откат обязан вернуть plaintext"
    assert row["api_key_hash"] == _digest(account["raw"]), "откат обязан вернуть старый хэш"


def test_pg_wrapper_rolls_back_when_tenants_rowcount_is_zero(monkeypatch):
    """(ж) PG-ветка: rowcount != 1 → rollback, commit не вызывается."""
    import db_pg_sync

    class _FakeConn:
        def __init__(self):
            self.rolled_back = False
            self.committed = False

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            self.committed = True

    conn = _FakeConn()
    monkeypatch.setattr(db_adapter, "_pg_enabled", lambda: True)
    monkeypatch.setattr(db_adapter, "_pg_conn", lambda: conn)
    monkeypatch.setattr(db_adapter, "_pg_return", lambda *_a, **_k: None)
    monkeypatch.setattr(
        db_pg_sync,
        "rotate_user_api_key",
        lambda *_a, **_k: {"users_count": 1, "tenants_count": 0, "old_api_key": "old"},
    )

    with pytest.raises(RuntimeError):
        db_adapter.rotate_user_api_key("user-x", "tenant-x", "roma-newkey1234")

    assert conn.rolled_back is True
    assert conn.committed is False


def test_rotation_failure_is_500_without_details(account, client, monkeypatch):
    """(з) сбой транзакции → 500 и ни строчки о ключах в теле ответа."""

    def _boom(*_a, **_k):
        raise RuntimeError("rotate_user_api_key: expected one tenants row, got 0")

    monkeypatch.setattr(db_adapter, "rotate_user_api_key", _boom)

    session_id, token = _login(client, account["tenant_id"])
    try:
        resp = client.post("/account/rotate-key", data={"csrf_token": token})
        assert resp.status_code == 500, resp.text
        assert "expected one tenants row" not in resp.text
    finally:
        delete_session(session_id)


def test_logs_carry_ids_only_never_the_key(account, client, caplog, monkeypatch):
    """(и) в логах — tenant_id/user_id, ни старого, ни нового ключа.

    ``main`` ставит ``logger.propagate = False`` (свой JSON-хендлер), поэтому
    запись поднимается к caplog только при включённом propagate.
    """
    monkeypatch.setattr(deps.logger, "propagate", True)
    session_id, token = _login(client, account["tenant_id"])
    try:
        with caplog.at_level("INFO", logger="roma"):
            resp = client.post("/account/rotate-key", data={"csrf_token": token})
        assert resp.status_code == 200, resp.text
        new_key = NEW_KEY.search(resp.text).group(1)

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert new_key not in logged, "новый ключ попал в лог"
        assert account["raw"] not in logged, "старый ключ попал в лог"
        assert account["tenant_id"] in logged and account["user_id"] in logged
    finally:
        delete_session(session_id)


def test_form_is_rendered_with_session_bound_token(account, client):
    """(к) GET /account отдаёт форму с токеном этой сессии."""
    session_id, token = _login(client, account["tenant_id"])
    try:
        resp = client.get("/account")
        assert resp.status_code == 200, resp.text
        assert 'name="csrf_token"' in resp.text
        assert token in resp.text
        assert 'action="/account/rotate-key"' in resp.text
    finally:
        delete_session(session_id)


def _rotate_route():
    for route in main.app.routes:
        if getattr(route, "path", "") == "/account/rotate-key":
            return route
    raise AssertionError("route /account/rotate-key is not registered")


def test_limiter_is_inside_the_router_decorator():
    """(л) slowapi обёрнут роутером, иначе объявленный лимит декоративен."""
    assert hasattr(_rotate_route().endpoint, "__wrapped__"), (
        "@limiter.limit must sit below @router.post, or 5/15minutes never runs"
    )


def test_sixth_request_in_the_window_is_429(account, client, monkeypatch):
    """(л) пять ротаций проходят, шестая — 429: '5/15minutes' реально парсится."""
    monkeypatch.setattr(
        db_adapter,
        "get_user_by_tenant",
        lambda _tid: {
            "id": account["user_id"],
            "provider": "google",
            "tenant_id": account["tenant_id"],
            "api_key": account["raw"],
        },
    )
    monkeypatch.setattr(db_adapter, "get_tenant", lambda _tid: {"plan": "free"})
    monkeypatch.setattr(
        db_adapter,
        "rotate_user_api_key",
        lambda *_a, **_k: {"users_count": 1, "tenants_count": 1, "old_api_key": ""},
    )

    session_id, token = _login(client, account["tenant_id"])
    try:
        codes = [client.post("/account/rotate-key", data={"csrf_token": token}).status_code for _ in range(6)]
    finally:
        delete_session(session_id)

    assert codes[:5] == [200] * 5, codes
    assert codes[5] == 429, codes
