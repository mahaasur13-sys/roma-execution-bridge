"""SEC: /admin* and /billing/top-up are gated by ROMA_ADMIN_KEYS, not tenant-demo.

Every gated route is checked for the four rejection paths and the one acceptance
path:

  - no key                        -> 401
  - tenant / tenant-demo key      -> 403
  - key from ROMA_ADMIN_KEYS      -> 200
  - empty ROMA_ADMIN_KEYS         -> 403 (fail-closed)
  - X-Forwarded-For spoofing a
    trusted IP without
    ROMA_TRUST_PROXY=1            -> 403
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import deps
import main

# (method, path, json body) for every route that must sit behind _admin_only.
ADMIN_ROUTES = [
    ("GET", "/admin", None),
    ("GET", "/admin/backends", None),
    ("GET", "/admin/analytics", None),
    ("GET", "/admin/analytics/users", None),
    ("GET", "/admin/analytics/events", None),
    ("GET", "/admin/feedback", None),
    ("GET", "/admin/email-stats", None),
    ("GET", "/admin/invites", None),
    ("GET", "/admin/verification-stats", None),
    ("POST", "/admin/invites/create", {"max_uses": 1}),
    ("POST", "/admin/invites/deactivate", {"code": "no-such-code"}),
    ("POST", "/admin/invite", {"dry_run": True}),
    ("POST", "/admin/test-alert", {}),
    ("POST", "/billing/top-up", {"tenant_id": "t-none", "amount": 1.0}),
]

ADMIN_KEY = "test-admin-key-" + uuid.uuid4().hex
TENANT_KEY = "test-tenant-key-" + uuid.uuid4().hex
DEMO_KEY = "test-demo-key-" + uuid.uuid4().hex
TENANT_ID = "t-" + uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Admin key configured, IP allowlist open, background worker disabled."""

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    monkeypatch.setenv("ROMA_ADMIN_KEYS", f" {ADMIN_KEY} ,")
    monkeypatch.setattr(deps, "ADMIN_IP_ALLOWLIST", "*")

    db.seed_tenants({TENANT_KEY: {"tenant_id": TENANT_ID, "name": "tenant"}})
    demo_tenant = "tenant-demo"
    db.seed_tenants({DEMO_KEY: {"tenant_id": demo_tenant, "name": "demo"}})
    original = dict(main.API_KEYS)
    main.API_KEYS.update(
        {
            TENANT_KEY: {"tenant_id": TENANT_ID},
            DEMO_KEY: {"tenant_id": demo_tenant},
        }
    )
    yield
    main.API_KEYS.clear()
    main.API_KEYS.update(original)


def _call(client, method, path, body, headers):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body or {}, headers=headers)


def _ids(routes):
    return [f"{m} {p}" for m, p, _ in routes]


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES, ids=_ids(ADMIN_ROUTES))
def test_no_key_401(method, path, body):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _call(client, method, path, body, {})
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES, ids=_ids(ADMIN_ROUTES))
def test_tenant_key_403(method, path, body):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _call(client, method, path, body, {"X-API-Key": TENANT_KEY})
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES, ids=_ids(ADMIN_ROUTES))
def test_tenant_demo_key_403(method, path, body):
    """tenant-demo lost its admin rights — no compatibility flag brings it back."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _call(client, method, path, body, {"X-API-Key": DEMO_KEY})
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES, ids=_ids(ADMIN_ROUTES))
def test_admin_key_passes_gate_200(method, path, body):
    """An admin key gets past the gate; the handler's own status may differ."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _call(client, method, path, body, {"X-API-Key": ADMIN_KEY})
    assert resp.status_code not in (401, 403), f"{method} {path} -> {resp.status_code}"


def test_admin_key_200_on_gate_only_route():
    """GET /admin/verification-stats returns 200 for an admin key (no DB gate)."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/admin/verification-stats", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200, resp.text


def test_test_alert_sends_only_for_admin_key(monkeypatch):
    """POST /admin/test-alert: an admin key reaches the handler and sends once."""
    from routers import admin as admin_router

    sent = []
    monkeypatch.setattr(admin_router.alert_dispatcher, "send", sent.append)
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = client.post("/admin/test-alert", json={"message": "ping"}, headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sent"] is True
    assert len(sent) == 1

    for key in (TENANT_KEY, DEMO_KEY, None):
        resp = client.post(
            "/admin/test-alert",
            json={"message": "ping"},
            headers={"X-API-Key": key} if key else {},
        )
        assert resp.status_code in (401, 403), f"key={key} -> {resp.status_code}"
    assert len(sent) == 1, "a rejected request must not dispatch an alert"


def test_admin_key_on_top_up_writes_ledger():
    """The acceptance path really reaches the handler: top-up credits the target."""
    target = "t-" + uuid.uuid4().hex[:8]
    before = main.billing_ledger.get_balance(target)
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": target, "amount": 2.5},
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert resp.status_code == 200, resp.text
    assert main.billing_ledger.get_balance(target) == pytest.approx(before + 2.5)


@pytest.mark.parametrize("method,path,body", ADMIN_ROUTES, ids=_ids(ADMIN_ROUTES))
def test_empty_admin_keys_fail_closed_403(monkeypatch, method, path, body):
    monkeypatch.delenv("ROMA_ADMIN_KEYS", raising=False)
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _call(client, method, path, body, {"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"


def test_query_param_api_key_not_accepted():
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get(f"/admin/verification-stats?api_key={ADMIN_KEY}")
    assert resp.status_code == 401


def test_spoofed_xff_without_trust_proxy_403(monkeypatch):
    """XFF claiming 127.0.0.1 must not smuggle a request into the allowlist."""
    monkeypatch.delenv("ROMA_TRUST_PROXY", raising=False)
    monkeypatch.setattr(deps, "ADMIN_IP_ALLOWLIST", "127.0.0.1,::1")
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get(
        "/admin/verification-stats",
        headers={"X-API-Key": ADMIN_KEY, "X-Forwarded-For": "127.0.0.1"},
    )
    assert resp.status_code == 403, resp.text


def test_trusted_xff_honoured_when_trust_proxy_on(monkeypatch):
    monkeypatch.setenv("ROMA_TRUST_PROXY", "1")
    monkeypatch.setattr(deps, "ADMIN_IP_ALLOWLIST", "127.0.0.1,::1")
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get(
        "/admin/verification-stats",
        headers={"X-API-Key": ADMIN_KEY, "X-Forwarded-For": "127.0.0.1, 10.1.2.3"},
    )
    assert resp.status_code == 200, resp.text
