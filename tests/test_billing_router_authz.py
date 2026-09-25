"""Negative authz + isolation tests for the extracted billing router.

These exercise the authz surface of ``routers/billing.py`` (not just path
presence), so a lost ``Depends(verify_api_key)`` or a missing ``_admin_only``
call is caught:

  - POST /billing/top-up without X-API-Key -> 401
  - POST /billing/top-up with a non-admin key -> 403
  - GET  /billing/ledger scopes entries to the caller's tenant only
  - GET  /billing/balance without X-API-Key -> 401

Run with the project venv:
    /home/felix/dsh-workspace/.venv-roma/bin/python -m pytest tests/test_billing_router_authz.py -q
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import main


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    """Prevent startup from launching the infinite poll_and_execute loop."""
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def tenants(monkeypatch):
    """Seed two distinct tenants A/B with keys, and bypass email verification."""
    a_id = _uniq("t-a")
    b_id = _uniq("t-b")
    key_a = _uniq("key-a")
    key_b = _uniq("key-b")

    db.seed_tenants({
        key_a: {"tenant_id": a_id, "name": "A"},
        key_b: {"tenant_id": b_id, "name": "B"},
    })
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)

    # _admin_only / verify_api_key read the in-memory API_KEYS registry.
    original = dict(main.API_KEYS)
    main.API_KEYS.update({
        key_a: {"tenant_id": a_id},
        key_b: {"tenant_id": b_id},
    })
    main.API_KEYS["hotfix-admin-demo"] = {"tenant_id": "tenant-demo"}

    yield {"a_id": a_id, "b_id": b_id, "key_a": key_a, "key_b": key_b}

    main.API_KEYS.clear()
    main.API_KEYS.update(original)


def test_top_up_without_api_key_401(tenants):
    """POST /billing/top-up with no X-API-Key must be rejected (401)."""
    client = TestClient(main.app, raise_server_exceptions=False)
    # X-Forwarded-For places us inside the admin IP allowlist so the check
    # reaches the API-key gate (rather than failing on IP).
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": tenants["b_id"], "amount": 10.0},
        headers={"X-Forwarded-For": "127.0.0.1"},
    )
    assert resp.status_code == 401


def test_top_up_non_admin_403(tenants):
    """A non-admin key (tenant != tenant-demo) must not top up (403)."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": tenants["b_id"], "amount": 10.0},
        headers={"X-API-Key": tenants["key_a"], "X-Forwarded-For": "127.0.0.1"},
    )
    assert resp.status_code == 403


def test_ledger_returns_only_caller_entries(tenants):
    """GET /billing/ledger must scope entries to the caller's tenant."""
    # Seed a distinct credit for A and B so we can detect cross-tenant leakage.
    main.billing_ledger.credit(tenants["a_id"], 123.45, note="a-only")
    main.billing_ledger.credit(tenants["b_id"], 678.90, note="b-only")

    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/billing/ledger", headers={"X-API-Key": tenants["key_a"]})
    assert resp.status_code == 200

    amounts = {e["amount"] for e in resp.json()["entries"]}
    assert 123.45 in amounts       # caller's own credit is visible
    assert 678.90 not in amounts   # other tenant's credit is not leaked


def test_balance_without_api_key_401(tenants):
    """GET /billing/balance with no X-API-Key must be rejected (401)."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/billing/balance")
    assert resp.status_code == 401
