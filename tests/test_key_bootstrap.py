"""PR-F2d: ключ выдаётся один раз — при подтверждении адреса или новом OAuth.

Контракт:

  * signup и OAuth-INSERT пишут ``users.api_key = ''`` и только дайджест:
    плейнтекст живёт в ответе, а не в строке таблицы;
  * ``GET /auth/verify-email`` при первом успехе выпускает новый ключ, меняет
    ``users.api_key_hash`` и ``tenants.api_key_hash`` одной транзакцией, отдаёт
    ключ в HTML с ``Cache-Control: no-store`` и ставит сессию;
  * повторный переход по той же ссылке ключ не показывает (токен израсходован
    ``mark_email_verified``), OAuth-new показывает ключ только в ответе callback;
  * ``scripts/rotate_user_key.py`` — тот же путь без HTTP: файл 0400, в stdout
    только путь, ``--dry-run`` ничего не меняет; ключ не попадает в логи.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db
import db_adapter
import deps
import main
import routers.auth as auth_routes
from auth.sessions import _sessions, delete_session, get_session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_IN_HTML = re.compile(r"<code>(roma-[0-9a-f]{8}-[0-9a-f]{3})</code>")


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row(table: str, where: str, value: str) -> dict | None:
    c = db._conn()
    try:
        row = c.execute(f"SELECT * FROM {table} WHERE {where} = ?", (value,)).fetchone()
    finally:
        c.close()
    return dict(row) if row else None


def _pg():
    import psycopg2

    return psycopg2.connect(os.environ["PG_DSN"])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Фоновый воркер выключен, реестр ключей и лимитер чистые на входе и выходе."""

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
def oauth_user(sqlite_tmp_db):
    """OAuth-пользователь в sqlite-ветке: ключ выпущен «по старой схеме»."""
    adapter = sqlite_tmp_db
    db.init_db()
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
    raw = f"roma-{uuid.uuid4().hex[:12]}"

    adapter.seed_tenants({raw: {"tenant_id": tenant_id, "name": "bootstrap"}})
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
    for sid in [s for s, v in _sessions.items() if v.get("tenant_id") == tenant_id]:
        delete_session(sid)


@pytest.fixture
def client():
    return TestClient(main.app)


def test_oauth_insert_stores_only_the_digest(sqlite_tmp_db, monkeypatch):
    """(б) OAuth-INSERT: плейнтекст в строку не попадает — там пусто и дайджест."""
    adapter = sqlite_tmp_db
    db.init_db()
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
    raw = f"roma-{uuid.uuid4().hex[:12]}"

    adapter.seed_tenants({raw: {"tenant_id": tenant_id, "name": "signup-shape"}})
    adapter.upsert_oauth_user(user_id, f"{user_id}@example.com", "Test", "google", tenant_id, raw)
    try:
        row = _row("users", "id", user_id)
        assert row is not None
        assert row["api_key"] == "", "плейнтекст в users.api_key не остаётся"
        assert row["api_key_hash"] == _digest(raw)
    finally:
        c = db._conn()
        try:
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
            c.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
            c.commit()
        finally:
            c.close()


def test_verify_email_shows_key_once_and_moves_both_hashes(oauth_user, client, monkeypatch, caplog):
    """(а) первый успех: ключ в HTML один раз, оба хэша = sha256(new), сессия, no-store."""
    token = "verify-token-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(auth_routes, "verify_token", lambda _t: ({"user_id": oauth_user["user_id"]}, "success"))

    with caplog.at_level("DEBUG"):
        resp = client.get(f"/auth/verify-email?token={token}")

    assert resp.status_code == 200, resp.text
    assert resp.headers.get("cache-control") == "no-store"
    match = KEY_IN_HTML.search(resp.text)
    assert match, f"ключ не показан: {resp.text[:300]}"
    new_key = match.group(1)
    assert new_key != oauth_user["raw"]

    user = _row("users", "id", oauth_user["user_id"])
    tenant = _row("tenants", "id", oauth_user["tenant_id"])
    assert user["api_key"] == ""
    assert user["api_key_hash"] == _digest(new_key)
    assert tenant["api_key_hash"] == user["api_key_hash"], "users и tenants меняются вместе"

    session_id = resp.cookies.get("session_id")
    assert session_id and get_session(session_id)["tenant_id"] == oauth_user["tenant_id"]
    assert deps.lookup_key(new_key) is not None
    assert deps.lookup_key(oauth_user["raw"]) is None, "старый ключ снят с реестра"
    assert new_key not in caplog.text, "ключ не должен попадать в лог"


def test_verified_link_replay_has_no_key(oauth_user, client, monkeypatch):
    """(а) токен израсходован: повторный переход отвечает 400 и ключа не показывает."""
    monkeypatch.setattr(auth_routes, "verify_token", lambda _t: ({"user_id": oauth_user["user_id"]}, "success"))
    first = client.get("/auth/verify-email?token=spent")
    assert first.status_code == 200
    digest_after_first = _row("users", "id", oauth_user["user_id"])["api_key_hash"]

    # Ровно то, что делает mark_email_verified: токен обнулён → verify_token даёт invalid.
    monkeypatch.setattr(auth_routes, "verify_token", lambda _t: (None, "invalid"))
    replay = client.get("/auth/verify-email?token=spent")

    assert replay.status_code == 400
    assert KEY_IN_HTML.search(replay.text) is None
    assert _row("users", "id", oauth_user["user_id"])["api_key_hash"] == digest_after_first


def test_oauth_new_user_reveals_key_once(monkeypatch, sqlite_tmp_db):
    """(б) OAuth-new: ключ в ответе callback один раз, существующий — редирект."""
    db.init_db()
    info = {
        "id": f"user-{uuid.uuid4().hex[:8]}",
        "email": f"{uuid.uuid4().hex[:8]}@example.com",
        "name": "OAuth Test",
        "provider": "google",
    }

    async def _fake_callback(_code):
        return info

    monkeypatch.setattr(auth_routes, "OAUTH_ENABLED", True)
    monkeypatch.setattr(auth_routes, "_oauth_google_callback", _fake_callback)

    new_client = TestClient(main.app)
    try:
        resp = new_client.get("/auth/oauth/callback/google?code=x")
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("cache-control") == "no-store"
        match = KEY_IN_HTML.search(resp.text)
        assert match, f"ключ не показан при OAuth-new: {resp.text[:300]}"
        assert resp.cookies.get("session_id")

        row = _row("users", "id", info["id"])
        assert row is not None
        assert row["api_key"] == ""
        assert row["api_key_hash"] == _digest(match.group(1))

        again = TestClient(main.app).get("/auth/oauth/callback/google?code=x", follow_redirects=False)
        assert again.status_code == 302, "существующий пользователь уходит на /dashboard"
        assert KEY_IN_HTML.search(again.text) is None
    finally:
        if _row("users", "id", info["id"]):
            c = db._conn()
            try:
                tenant_id = _row("users", "id", info["id"])["tenant_id"]
                c.execute("DELETE FROM users WHERE id = ?", (info["id"],))
                c.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
                c.commit()
            finally:
                c.close()


@pytest.fixture
def rotate_cli(monkeypatch, tmp_path):
    """Скрипт как модуль + /root/.sealed подменён на tmp, чтобы не писать в живой каталог."""
    path = os.path.join(ROOT, "scripts", "rotate_user_key.py")
    spec = importlib.util.spec_from_file_location("rotate_user_key", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SEALED_DIR", str(tmp_path))
    return module


def test_cli_dry_run_changes_nothing(oauth_user, rotate_cli, tmp_path, capsys):
    """CLI --dry-run: ни строки, ни файла."""
    before = _row("users", "id", oauth_user["user_id"])

    rc = rotate_cli.main(["--user-id", oauth_user["user_id"], "--dry-run"])

    assert rc == 0
    assert _row("users", "id", oauth_user["user_id"])["api_key_hash"] == before["api_key_hash"]
    assert list(tmp_path.iterdir()) == []
    assert "dry-run" in capsys.readouterr().out


def test_cli_rotation_writes_0400_file_and_prints_only_path(oauth_user, rotate_cli, tmp_path, capsys, caplog):
    """CLI: файл 0400 с новым ключом, в stdout только путь, ключ не в логах."""
    with caplog.at_level("DEBUG"):
        rc = rotate_cli.main(["--user-id", oauth_user["user_id"]])

    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out.startswith(str(tmp_path)) and out.count("\n") == 0, "печатается только путь"

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name.startswith(f"rotated-{oauth_user['user_id'][:8]}-")
    assert (files[0].stat().st_mode & 0o777) == 0o400

    new_key = files[0].read_text().strip()
    assert new_key.startswith("roma-")
    assert new_key not in out
    assert new_key not in caplog.text

    user = _row("users", "id", oauth_user["user_id"])
    assert user["api_key"] == ""
    assert user["api_key_hash"] == _digest(new_key)
    assert _row("tenants", "id", oauth_user["tenant_id"])["api_key_hash"] == user["api_key_hash"]


@pytest.mark.pg
def test_verify_email_end_to_end_on_pg(client, monkeypatch, caplog):
    """PG: настоящий токен → ключ выдан один раз, повторный переход 400, хэши совпадают."""
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
    email = f"{user_id}@example.com"
    raw = f"roma-{uuid.uuid4().hex[:12]}"
    token = "verify-" + uuid.uuid4().hex

    db_adapter.seed_tenants({raw: {"tenant_id": tenant_id, "name": "bootstrap-pg"}})
    db_adapter.create_user_with_password(
        user_id, email, "Test", tenant_id, raw, "pbkdf2-stub", token, "2030-01-01T00:00:00Z"
    )

    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute("SELECT api_key, api_key_hash FROM users WHERE id = %s", (user_id,))
        stored_plain, stored_hash = cur.fetchone()
        assert stored_plain == "", "signup пишет пустой api_key"
        assert stored_hash == _digest(raw)
    finally:
        conn.close()

    with caplog.at_level("DEBUG"):
        resp = client.get(f"/auth/verify-email?token={token}")

    try:
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("cache-control") == "no-store"
        match = KEY_IN_HTML.search(resp.text)
        assert match, f"ключ не показан: {resp.text[:300]}"
        new_key = match.group(1)
        assert new_key != raw
        assert new_key not in caplog.text

        conn = _pg()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT u.api_key, u.api_key_hash, u.email_verified, u.verification_token, "
                "t.api_key_hash FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE u.id = %s",
                (user_id,),
            )
            plain, user_hash, verified, tok, tenant_hash = cur.fetchone()
        finally:
            conn.close()

        assert plain == ""
        assert user_hash == _digest(new_key) == tenant_hash
        assert verified is True
        assert tok is None, "токен израсходован"

        replay = client.get(f"/auth/verify-email?token={token}")
        assert replay.status_code == 400
        assert KEY_IN_HTML.search(replay.text) is None
    finally:
        conn = _pg()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
            conn.commit()
        finally:
            conn.close()


@pytest.mark.pg
def test_signup_endpoint_writes_empty_plaintext(client, monkeypatch, caplog):
    """(б) POST /auth/signup: в users остаются пустой api_key и дайджест."""
    email = f"signup-{uuid.uuid4().hex[:8]}@example.com"

    monkeypatch.setattr(auth_routes, "BETA_MODE", False)
    monkeypatch.setattr(auth_routes, "BETA_REQUIRE_INVITE", False)

    with caplog.at_level("DEBUG"):
        resp = client.post("/auth/signup", json={"email": email, "password": "correct-horse-1"})

    assert resp.status_code == 201, resp.text

    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, tenant_id, api_key, api_key_hash FROM users WHERE email = %s", (email,))
        user_id, tenant_id, plain, digest = cur.fetchone()
        try:
            assert plain == "", "signup не хранит плейнтекст"
            assert re.fullmatch(r"[0-9a-f]{64}", digest), "в api_key_hash лежит sha256-hex"
            assert digest not in caplog.text
        finally:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
            conn.commit()
    finally:
        conn.close()
