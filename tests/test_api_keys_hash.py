"""SEC: реестр API-ключей хранит только digest, а не сырой ключ.

Контракт (PR-F2a):

  * ``deps.API_KEYS`` индексируется sha256-дайджестом: сырой ключ в реестр не попадает,
    поэтому heap dump / repr / лог не отдаёт рабочий креденшл;
  * ``lookup_key(raw)`` находит запись по сырому ключу, ``lookup_key(chuzhoy)`` → ``None``;
  * ``/dashboard`` принимает ключ только заголовком ``X-API-Key``: ``?api_key=`` → 401
    (query утекает в access-логи и Referer);
  * source-pin: в ``main.py`` и ``routers/`` больше нет прямых обращений к реестру
    (``in API_KEYS`` / ``API_KEYS[...]``) — только через ``lookup_key``/``register_key``.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import deps
import main
from deps import key_digest, lookup_key, register_key

REPO_ROOT = Path(__file__).resolve().parent.parent
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Реестр — процесс-глобальный: каждый тест начинается и заканчивается пустым."""
    original = dict(deps.API_KEYS)
    deps.API_KEYS.clear()
    yield
    deps.API_KEYS.clear()
    deps.API_KEYS.update(original)


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


def test_registry_holds_digests_only():
    """(а) после регистрации в реестре нет сырого ключа — только 64 hex."""
    raw = _uniq("roma-key")
    register_key(raw, {"tenant_id": "tenant-x"})

    assert raw not in deps.API_KEYS, "сырой ключ не должен лежать в реестре"
    assert list(deps.API_KEYS) == [key_digest(raw)]
    assert all(HEX64.match(k) for k in deps.API_KEYS), "ключи реестра обязаны быть sha256-дайджестами"


def test_lookup_key_resolves_raw_and_rejects_wrong():
    """(б) lookup_key находит по сырому ключу и не находит чужой/пустой."""
    raw = _uniq("roma-key")
    info = {"tenant_id": "tenant-y", "plan": "free"}
    register_key(raw, info)

    assert lookup_key(raw) is info
    assert lookup_key(_uniq("roma-other")) is None
    assert lookup_key("") is None
    assert lookup_key(None) is None


def test_dashboard_rejects_query_key_and_accepts_header(monkeypatch):
    """(в) /dashboard с ?api_key= → 401, с заголовком X-API-Key → 200.

    Usage-агрегат замокан: проверяется авторизация, а не рендер графиков.
    """
    raw = _uniq("roma-key")
    tenant_id = _uniq("test-dash")

    db.seed_tenants({raw: {"tenant_id": tenant_id, "name": "dash"}})
    register_key(raw, {"tenant_id": tenant_id, "plan": "free"})
    monkeypatch.setattr(
        main,
        "_get_tenant_usage",
        lambda _tid: {"total_jobs": 0, "gpu_seconds": 0, "gpu_hours": 0, "total_cost": 0.0},
    )

    client = TestClient(main.app, raise_server_exceptions=False)

    by_query = client.get("/dashboard", params={"api_key": raw})
    assert by_query.status_code == 401, by_query.text

    by_header = client.get("/dashboard", headers={"X-API-Key": raw})
    assert by_header.status_code == 200, by_header.text
    assert tenant_id in by_header.text


def test_no_direct_registry_access_in_source():
    """(г) source-pin: main.py и routers/ ходят в реестр только через хелперы."""
    offenders: list[str] = []
    targets = [REPO_ROOT / "main.py", *sorted((REPO_ROOT / "routers").glob("*.py"))]
    for path in targets:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if "in API_KEYS" in line or "API_KEYS[" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert offenders == [], "прямые обращения к реестру запрещены:\n" + "\n".join(offenders)


def test_dashboard_session_cookie_path(monkeypatch):
    """(д) Cookie-сессия — отдельный вход в /dashboard, ключ не нужен.

    Фактическое поведение мастер-кода сохраняется: без сессии и без заголовка
    роут отдаёт 302 на /auth/login (страница входа), а не 401.
    """
    from auth.sessions import create_session, delete_session

    tenant_id = _uniq("test-sess")
    raw = _uniq("roma-key")

    db.seed_tenants({raw: {"tenant_id": tenant_id, "name": "sess"}})
    register_key(raw, {"tenant_id": tenant_id, "plan": "free"})
    monkeypatch.setattr(
        main,
        "_get_tenant_usage",
        lambda _tid: {"total_jobs": 0, "gpu_seconds": 0, "gpu_hours": 0, "total_cost": 0.0},
    )

    session_id = create_session(tenant_id)
    client = TestClient(main.app, raise_server_exceptions=False)

    try:
        anonymous = client.get("/dashboard", follow_redirects=False)
        assert anonymous.status_code == 302, anonymous.text
        assert anonymous.headers["location"] == "/auth/login"

        client.cookies.set("session_id", "no-such-session")
        unknown = client.get("/dashboard", follow_redirects=False)
        assert unknown.status_code == 302, unknown.text
        assert unknown.headers["location"] == "/auth/login"

        client.cookies.set("session_id", session_id)
        with_session = client.get("/dashboard")
        assert with_session.status_code == 200, with_session.text
        assert tenant_id in with_session.text
    finally:
        delete_session(session_id)
