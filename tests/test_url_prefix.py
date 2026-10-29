"""feat/url-prefix: сервис живёт под префиксом (live — ``/roma``).

Контракт:

  * ``config.u(path)`` = ``ROMA_URL_PREFIX`` + path; без переменной префикс пуст
    и все ссылки остаются корневыми (поведение до PR не меняется);
  * префикс нормализуется (``roma`` → ``/roma``, ``/roma/`` → ``/roma``) и
    валидируется: только ``/``, буквы, цифры, ``_``, ``-``; мусор или ``//``
    дают ValueError ещё при импорте config — старт падает, а не первый клик;
  * ссылки, ``action=`` форм и ``Location`` редиректов несут тот же префикс,
    иначе за прокси они уезжают в корень домена;
  * ``/stats/daily`` больше не публичный: нужна сессия или ``X-API-Key``,
    а данные ограничены тенантом вызывающего (раньше отдавались глобальные
    агрегаты по всем тенантам).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import config
import db
import db_adapter
import deps
import main
import routers.auth as auth_routes
from auth.sessions import create_session, delete_session


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Воркер выключен, реестр и лимитер чистые; префикс не протекает из окружения."""

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)
    monkeypatch.delenv("ROMA_URL_PREFIX", raising=False)

    original = dict(deps.API_KEYS)
    deps.API_KEYS.clear()
    deps.limiter._storage.reset()
    yield
    deps.limiter._storage.reset()
    deps.API_KEYS.clear()
    deps.API_KEYS.update(original)


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def tenants(sqlite_tmp_db):
    """Два тенанта с рабочими ключами в sqlite-ветке."""
    adapter = sqlite_tmp_db
    db.init_db()
    made = []
    for name in ("alpha", "beta"):
        tenant_id = f"tenant-{uuid.uuid4().hex[:8]}"
        raw = f"roma-key-{uuid.uuid4().hex[:8]}"
        adapter.seed_tenants({raw: {"tenant_id": tenant_id, "name": name}})
        deps.register_key(raw, {"tenant_id": tenant_id, "plan": "free"})
        made.append({"tenant_id": tenant_id, "raw": raw})

    yield {"alpha": made[0], "beta": made[1]}

    c = db._conn()
    try:
        for item in made:
            c.execute("DELETE FROM tenants WHERE id = ?", (item["tenant_id"],))
        c.commit()
    finally:
        c.close()


def test_prefix_empty_keeps_root_links(client):
    """Без ROMA_URL_PREFIX ссылки остаются корневыми — регрессии нет."""
    login = client.get("/auth/login")
    assert login.status_code == 200, login.text
    assert 'action="/auth/login"' in login.text

    signup = client.get("/auth/signup")
    assert signup.status_code == 200, signup.text
    assert "fetch('/auth/signup'" in signup.text


def test_prefix_is_applied_to_links_and_forms(client, monkeypatch):
    """С префиксом те же страницы ссылаются на /roma/... и форма шлёт туда же."""
    monkeypatch.setenv("ROMA_URL_PREFIX", "/roma")
    # OAuth-кнопка рендерится только у настроенного провайдера
    # (fix/login-hide-oauth-when-disabled), иначе проверять префикс не на чем.
    monkeypatch.setattr(auth_routes, "GOOGLE_CLIENT_ID", "".join(["google", "-", "client", "-", "id"]))
    monkeypatch.setattr(auth_routes, "OAUTH_ENABLED", True)

    login = client.get("/auth/login")
    assert login.status_code == 200, login.text
    assert 'action="/roma/auth/login"' in login.text
    assert 'href="/roma/auth/oauth/login/google"' in login.text
    assert 'action="/auth/login"' not in login.text

    signup = client.get("/auth/signup")
    assert signup.status_code == 200, signup.text
    assert "fetch('/roma/auth/signup'" in signup.text
    assert 'href="/roma/auth/login"' in signup.text

    beta = client.get("/beta")
    assert beta.status_code == 200, beta.text
    assert "fetch('/roma/beta/apply'" in beta.text


def test_redirects_carry_prefix(client, tenants, monkeypatch):
    """Location у неавторизованного дашборда, логина и logout несёт префикс."""
    monkeypatch.setenv("ROMA_URL_PREFIX", "/roma")
    alpha = tenants["alpha"]

    anon = client.get("/dashboard", follow_redirects=False)
    assert anon.status_code == 302
    assert anon.headers["location"] == "/roma/auth/login"

    logged_in = client.post("/auth/login", data={"api_key": alpha["raw"]}, follow_redirects=False)
    assert logged_in.status_code == 302, logged_in.text
    assert logged_in.headers["location"] == "/roma/dashboard"

    session_id = create_session(alpha["tenant_id"])
    try:
        client.cookies.set("session_id", session_id)
        again = client.get("/auth/login", follow_redirects=False)
        assert again.status_code == 302
        assert again.headers["location"] == "/roma/dashboard"

        out = client.get("/auth/logout", follow_redirects=False)
        assert out.status_code == 302
        assert out.headers["location"] == "/roma/auth/login"
    finally:
        delete_session(session_id)


def test_account_form_action_carries_prefix(client, tenants, monkeypatch):
    """Форма ротации ключа на /account тоже уходит под префикс."""
    monkeypatch.setenv("ROMA_URL_PREFIX", "/roma")
    alpha = tenants["alpha"]

    user_id = f"user-{uuid.uuid4().hex[:8]}"
    db_adapter.upsert_oauth_user(user_id, f"{user_id}@example.com", "T", "google", alpha["tenant_id"], alpha["raw"])
    session_id = create_session(alpha["tenant_id"])
    try:
        client.cookies.set("session_id", session_id)
        page = client.get("/account")
        assert page.status_code == 200, page.text
        assert 'action="/roma/account/rotate-key"' in page.text
        assert 'href="/roma/dashboard"' in page.text
    finally:
        delete_session(session_id)
        c = db._conn()
        try:
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
            c.commit()
        finally:
            c.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("/", ""), ("roma", "/roma"), ("/roma/", "/roma"), ("/roma/v2", "/roma/v2")],
)
def test_prefix_normalisation(raw, expected, monkeypatch):
    monkeypatch.setenv("ROMA_URL_PREFIX", raw)
    assert config.url_prefix() == expected
    assert config.u("/dashboard") == expected + "/dashboard"


@pytest.mark.parametrize("raw", ["/roma x", "/roma//x", "/roma!", "/roma?x=1", "/ro ma"])
def test_invalid_prefix_fails_at_start(raw, monkeypatch):
    """Мусор в префиксе — ValueError, а не тихо испорченные ссылки."""
    monkeypatch.setenv("ROMA_URL_PREFIX", raw)
    with pytest.raises(ValueError):
        config.url_prefix()


def test_stats_daily_requires_auth(client):
    """Без сессии и без ключа /stats/daily отвечает 401, а не общими цифрами."""
    resp = client.get("/stats/daily")
    assert resp.status_code == 401, resp.text


def test_stats_daily_is_scoped_to_the_caller(client, tenants, monkeypatch):
    """Тенант видит только свои ряды: чужие значения в ответ не попадают."""
    alpha, beta = tenants["alpha"], tenants["beta"]

    def fake_daily_stats(days: int = 7, tenant_id: str | None = None) -> dict:
        assert tenant_id is not None, "маршрут обязан передать тенанта в запрос"
        jobs = 7 if tenant_id == alpha["tenant_id"] else 999
        return {"dates": ["2026-10-27"], "jobs_count": [jobs], "gpu_hours": [1.5 if jobs == 7 else 99.0]}

    monkeypatch.setattr(db_adapter, "get_daily_stats", fake_daily_stats)

    session_id = create_session(alpha["tenant_id"])
    try:
        client.cookies.set("session_id", session_id)
        via_cookie = client.get("/stats/daily")
        assert via_cookie.status_code == 200, via_cookie.text
        assert via_cookie.json()["jobs_count"] == [7]
        assert 999 not in via_cookie.json()["jobs_count"]
    finally:
        delete_session(session_id)
        client.cookies.clear()

    via_key = client.get("/stats/daily", headers={"X-API-Key": beta["raw"]})
    assert via_key.status_code == 200, via_key.text
    assert via_key.json()["jobs_count"] == [999]
    assert 7 not in via_key.json()["jobs_count"]


def test_dashboard_quick_actions_carry_prefix_and_drop_dead_links(monkeypatch):
    """Ссылки дашборда: /health и /account под префиксом, мёртвых ссылок нет.

    /metrics и /docs закрыты снаружи (edge-hardening), /usage и /jobs в прокси
    не входят, /status/{job_id} не отдаёт страницу — значит и ссылок на них
    быть не должно. Рендерим без БД: подменяем только источник usage.
    """
    monkeypatch.setenv("ROMA_URL_PREFIX", "/roma")
    monkeypatch.setattr(main, "_get_tenant_usage", lambda tenant_id: {"total_jobs": 0})

    html = main._render_dashboard("tenant-x", "free")

    assert 'href="/roma/health"' in html
    assert 'href="/roma/account"' in html
    assert '"/roma/stats/daily"' in html
    for dead in ('href="/metrics"', 'href="/usage"', 'href="/jobs"', "/docs/quickstart.md"):
        assert dead not in html, f"мёртвая ссылка {dead} осталась в дашборде"
    assert "/status/" not in html, "детали задачи переехали в текст, страницы нет"


def test_session_cookie_path_follows_prefix(client, tenants, monkeypatch):
    """Cookie сессии живёт под точкой монтирования: Path=/roma, без префикса — Path=/."""
    alpha = tenants["alpha"]

    root = client.post("/auth/login", data={"api_key": alpha["raw"]}, follow_redirects=False)
    assert root.status_code == 302, root.text
    assert "Path=/;" in root.headers["set-cookie"]

    monkeypatch.setenv("ROMA_URL_PREFIX", "/roma")
    prefixed = client.post("/auth/login", data={"api_key": alpha["raw"]}, follow_redirects=False)
    assert prefixed.status_code == 302, prefixed.text
    assert "Path=/roma" in prefixed.headers["set-cookie"]

    out = client.get("/auth/logout", follow_redirects=False)
    assert out.status_code == 302
    assert "Path=/roma" in out.headers["set-cookie"], "delete_cookie должен чистить ту же Path"
