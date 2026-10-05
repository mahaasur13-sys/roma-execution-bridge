"""NF-1 regression: ``credit()`` must return the ``ledger_id`` of the entry it wrote.

Before the fix ``credit()`` (and ``append()``) returned ``None``, so every consumer
that captured the value — ``routers/billing.py:56`` (``entry_id``) and
``billing/stripe_client.py:127`` — silently propagated ``None``/empty for a money
event that DID get written.

Contract pinned here:
  * ``append()`` returns the generated ``ledger_id``;
  * ``credit()`` returns that same non-empty id (never ``None``/``""``);
  * the id matches the entry actually present in the ledger;
  * the id is still returned when the PG write fails (in-memory mirror survives);
  * ``POST /billing/top-up`` surfaces a non-empty ``entry_id`` in its response.

No network, no live PG: the ledger is an in-memory instance and PG writes are
monkeypatched. Money scope of other findings (F-001..F-005) is untouched.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

from billing.pg_ledger import PGUnavailableError, PGBillingLedger


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def ledger():
    return PGBillingLedger()


def _ids(ledger: PGBillingLedger, tenant_id: str) -> list[str]:
    return [e["ledger_id"] for e in ledger.get_tenant_entries(tenant_id)]


def test_credit_returns_non_empty_entry_id(ledger):
    tenant_id = _uniq("nf1")
    entry_id = ledger.credit(tenant_id, 5.0)

    assert isinstance(entry_id, str)
    assert entry_id != ""
    assert entry_id.startswith("led-")


def test_credit_id_matches_the_written_entry(ledger):
    tenant_id = _uniq("nf1")
    entry_id = ledger.credit(tenant_id, 5.0, note="top-up")

    assert entry_id in _ids(ledger, tenant_id)
    entry = next(e for e in ledger.get_tenant_entries(tenant_id) if e["ledger_id"] == entry_id)
    assert entry["type"] == "CREDIT"
    assert entry["amount"] == 5.0


def test_append_returns_entry_id(ledger):
    tenant_id = _uniq("nf1")
    entry_id = ledger.append(tenant_id, "CREDIT", 1.0)

    assert entry_id and entry_id in _ids(ledger, tenant_id)


def test_two_credits_return_distinct_ids(ledger):
    tenant_id = _uniq("nf1")
    first = ledger.credit(tenant_id, 1.0)
    second = ledger.credit(tenant_id, 2.0)

    assert {first, second} == set(_ids(ledger, tenant_id))
    assert len({first, second}) == 2


def test_entry_id_survives_pg_down(monkeypatch, ledger):
    """PG write failure must not turn a recorded credit into an empty entry_id."""

    def _boom(*args, **kwargs):
        raise PGUnavailableError("PG not configured or unavailable")

    monkeypatch.setattr(ledger, "_pg_execute", _boom)

    tenant_id = _uniq("nf1")
    entry_id = ledger.credit(tenant_id, 7.0)

    assert entry_id and entry_id in _ids(ledger, tenant_id)


def test_top_up_route_returns_non_empty_entry_id(monkeypatch):
    """Consumer check: the admin top-up response must not carry entry_id: null."""
    import routers.billing as billing_router

    fresh = PGBillingLedger()
    monkeypatch.setattr(billing_router, "billing_ledger", fresh)
    monkeypatch.setattr(
        billing_router,
        "_admin_only",
        lambda request: {"tenant_id": "tenant-demo", "name": "admin"},
    )

    import main

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)

    target = _uniq("nf1-target")
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(
        "/billing/top-up",
        json={"tenant_id": target, "amount": 3.0},
        headers={"X-API-Key": "test-admin-key"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entry_id"], f"entry_id must not be empty: {body!r}"
    assert body["entry_id"] in _ids(fresh, target)
