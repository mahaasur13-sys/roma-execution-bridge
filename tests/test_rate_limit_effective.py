"""PR-F1: the declared rate limits must actually run, and admin keys must be compared in constant time.

Two defects this file pins:

  1. ``@limiter.limit`` sat ABOVE ``@router.get``/``@router.post``, so the router
     registered the raw function and slowapi never wrapped it — every declared
     limit was decorative, and ``route.endpoint`` had no ``__wrapped__``. Fix:
     ``@router.xxx`` on top, ``@limiter.limit`` below it.
  2. ``verify_api_key`` (both copies) treated admin keys via
     ``x_api_key not in admin_keys()`` — an early-exit, non-constant-time set
     membership test. It must go through ``hmac.compare_digest``.

Limiter storage is reset before and after every test, so a 429 never leaks into
another test, and the seeded tenants are deleted in teardown.
"""

from __future__ import annotations

import hmac
import inspect
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import deps
import main
from routers import billing as billing_router

ADMIN_KEY = "test-admin-key-" + uuid.uuid4().hex
TENANT_KEY = "test-tenant-key-" + uuid.uuid4().hex
ADMIN_TENANT = "t-admin-" + uuid.uuid4().hex[:8]
TENANT_ID = "t-" + uuid.uuid4().hex[:8]

# (method, path, body, limit string) for every point that carries a limiter.
LIMITED_ROUTES = [
    ("POST", "/admin/test-alert", {}, 5),
    ("POST", "/admin/invite", {"dry_run": True}, 20),
    ("GET", "/admin/email-stats", None, 20),
    ("POST", "/billing/top-up", {"tenant_id": "t-none", "amount": 0.0}, 10),
    ("POST", "/feedback", {"rating": 5}, 10),
]

# The 8 routes converted to a Depends gate in this PR.
GATED_ROUTES = [
    ("GET", "/admin", None),
    ("GET", "/admin/analytics", None),
    ("GET", "/admin/analytics/users", None),
    ("GET", "/admin/analytics/events", None),
    ("GET", "/admin/feedback", None),
    ("GET", "/admin/email-stats", None),
    ("POST", "/admin/invite", {"dry_run": True}),
    ("POST", "/billing/top-up", {"tenant_id": "t-none", "amount": 0.0}),
]


def _purge_tenants(ids: tuple[str, ...]) -> None:
    """Delete the tenants this file seeded — no rows left behind (backlog nit)."""
    if db._pg_enabled():
        conn = db._pg_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM tenants WHERE id = ANY(%s)", (list(ids),))
            conn.commit()
        finally:
            db._pg_return(conn)
        return
    import db as sqlite_db

    c = sqlite_db._conn()
    c.execute(f"DELETE FROM tenants WHERE id IN ({','.join('?' * len(ids))})", ids)
    c.commit()
    c.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    monkeypatch.setenv("ROMA_ADMIN_KEYS", ADMIN_KEY)
    monkeypatch.setattr(deps, "ADMIN_IP_ALLOWLIST", "*")

    db.seed_tenants({ADMIN_KEY: {"tenant_id": ADMIN_TENANT, "name": "admin"}})
    db.seed_tenants({TENANT_KEY: {"tenant_id": TENANT_ID, "name": "tenant"}})

    deps.limiter._storage.reset()
    yield
    deps.limiter._storage.reset()
    _purge_tenants((ADMIN_TENANT, TENANT_ID))


def _route(path: str):
    for route in main.app.routes:
        if getattr(route, "path", "") == path:
            return route
    raise AssertionError(f"no route registered for {path}")


def _call(client, method, path, body, headers):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body or {}, headers=headers)


@pytest.mark.parametrize("method,path,body,limit", LIMITED_ROUTES, ids=[r[1] for r in LIMITED_ROUTES])
def test_limiter_wrapper_is_the_registered_endpoint(method, path, body, limit):
    """slowapi must be inside the router decorator, or the limit never runs."""
    endpoint = _route(path).endpoint
    assert hasattr(endpoint, "__wrapped__"), (
        f"{method} {path} is registered with the raw function — @limiter.limit({limit}/minute) is decorative"
    )


@pytest.mark.parametrize(
    "method,path,body,limit",
    [
        ("POST", "/admin/test-alert", {}, 5),
        ("POST", "/billing/top-up", {"tenant_id": "t-none", "amount": 1.0}, 10),
    ],
    ids=["test-alert", "top-up"],
)
def test_limit_returns_429_on_request_after_budget(monkeypatch, method, path, body, limit):
    """N allowed requests, then the N+1-th is 429 with the admin key."""
    if path == "/billing/top-up":
        monkeypatch.setattr(billing_router, "billing_ledger", _StubLedger())
        monkeypatch.setattr(billing_router, "write_event", lambda *a, **k: None)

    client = TestClient(main.app, raise_server_exceptions=False)
    headers = {"X-API-Key": ADMIN_KEY}
    codes = [_call(client, method, path, body, headers).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [200] * limit, codes
    assert codes[limit] == 429, f"{path}: expected 429 on request {limit + 1}, got {codes}"


class _StubLedger:
    """Keeps the 429 test off the ledger: only the gate and limiter are under test."""

    def credit(self, tenant_id, amount, note=""):
        return f"led-stub-{uuid.uuid4().hex[:8]}"

    def get_balance(self, tenant_id):
        return 0.0


def test_verify_api_key_compares_admin_keys_in_constant_time(monkeypatch):
    """Both ``verify_api_key`` copies must route admin keys through compare_digest."""
    calls = []
    real = hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", counting)

    assert deps.verify_api_key(ADMIN_KEY)["tenant_id"] == ADMIN_TENANT
    assert calls, "deps.verify_api_key skipped hmac.compare_digest"
    calls.clear()

    assert main.verify_api_key(ADMIN_KEY)["tenant_id"] == ADMIN_TENANT
    assert calls, "main.verify_api_key skipped hmac.compare_digest"


def test_admin_key_check_is_not_a_set_membership_test():
    """Source pin: the early-exit ``not in admin_keys()`` form must not come back."""
    for fn in (deps.verify_api_key, main.verify_api_key):
        src = inspect.getsource(fn)
        assert "not in admin_keys()" not in src, f"{fn.__module__}.{fn.__name__} uses set membership"
        assert "_admin_key_valid(" in src, f"{fn.__module__}.{fn.__name__} bypasses _admin_key_valid"


@pytest.mark.parametrize("method,path,body", GATED_ROUTES, ids=[f"{m} {p}" for m, p, _ in GATED_ROUTES])
def test_converted_routes_still_reject_non_admin(method, path, body):
    """Moving the gate into Depends must not change 401/403 behaviour."""
    client = TestClient(main.app, raise_server_exceptions=False)
    assert _call(client, method, path, body, {}).status_code == 401, f"{method} {path} without a key"
    assert _call(client, method, path, body, {"X-API-Key": TENANT_KEY}).status_code == 403, (
        f"{method} {path} with a tenant key"
    )


def test_depends_result_still_reaches_the_handler():
    """``admin_page`` renders the dashboard from the dependency's return value."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/admin", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
