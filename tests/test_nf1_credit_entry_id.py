"""NF-1 regression: ``credit()`` must return the ``ledger_id`` of the entry it wrote.

Before the fix ``credit()`` (and ``append()``) returned ``None``, so every consumer
that captured the value — ``routers/billing.py:56`` (``entry_id``) and
``billing/stripe_client.py:127`` — silently propagated ``None``/empty for a money
event that DID get written.

Contract pinned here:
  * ``append()`` returns the generated ``ledger_id``;
  * ``credit()`` returns that same non-empty id (never ``None``/``""``);
  * the id matches the entry actually present in the ledger;
  * ``credit()`` raises ``PGUnavailableError`` when the PG write fails (fail-closed);
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


def test_credit_raises_on_pg_down(monkeypatch, ledger):
    """PG write failure must raise PGUnavailableError (fail-closed)."""

    def _boom(*args, **kwargs):
        raise PGUnavailableError("PG not configured or unavailable")

    monkeypatch.setattr(ledger, "_pg_execute", _boom)

    tenant_id = _uniq("nf1")
    with pytest.raises(PGUnavailableError):
        ledger.credit(tenant_id, 7.0)


def test_top_up_route_returns_non_empty_entry_id(monkeypatch):
    """Consumer check: the admin top-up response must not carry entry_id: null."""
    import routers.billing as billing_router

    fresh = PGBillingLedger()
    monkeypatch.setattr(billing_router, "billing_ledger", fresh)
    # The gate is a route dependency, bound at import time, so it can no longer
    # be monkeypatched on the module: configure a real admin key instead.
    import deps

    monkeypatch.setenv("ROMA_ADMIN_KEYS", "test-admin-key")
    monkeypatch.setattr(deps, "ADMIN_IP_ALLOWLIST", "*")

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


def test_identical_entries_get_distinct_ids(ledger):
    """The id must not come from the built-in ``hash()``.

    ``hash()`` is salted per process (PYTHONHASHSEED), so the old
    ``led-{ms}-{hash(...)}`` scheme produced the SAME id for two identical
    entries recorded in the same millisecond — unreproducible across workers
    and a real unique-key collision. Same inputs must now yield distinct ids.
    """
    tenant_id = _uniq("nf1")
    first = ledger.append(tenant_id, "CREDIT", 4.0, "USD")
    second = ledger.append(tenant_id, "CREDIT", 4.0, "USD")

    assert first != second
    assert {first, second} == set(_ids(ledger, tenant_id))


def test_ledger_id_is_unique_across_many_appends(ledger):
    tenant_id = _uniq("nf1")
    ids = [ledger.credit(tenant_id, 1.0) for _ in range(50)]

    assert len(set(ids)) == 50
    assert set(ids) == set(_ids(ledger, tenant_id))


def test_pg_down_raises(monkeypatch, ledger):
    """A dropped PG write must raise PGUnavailableError (fail-closed), not silently pass."""

    def _boom(*args, **kwargs):
        raise PGUnavailableError("PG not configured or unavailable")

    monkeypatch.setattr(ledger, "_pg_execute", _boom)

    tenant_id = _uniq("nf1")
    with pytest.raises(PGUnavailableError):
        ledger.credit(tenant_id, 9.0)


@pytest.mark.pg
def test_credit_idempotent_same_key_returns_same_id(ledger):
    """Same idempotency_key must return the same ledger_id (no double credit)."""
    tenant_id = _uniq("idem")
    first = ledger.credit(tenant_id, 5.0, idempotency_key="topup-1")
    second = ledger.credit(tenant_id, 5.0, idempotency_key="topup-1")

    assert first == second
    assert len(_ids(ledger, tenant_id)) == 1


@pytest.mark.pg
def test_credit_idempotent_distinct_keys_return_distinct_ids(ledger):
    """Distinct idempotency keys must produce distinct ledger_ids."""
    tenant_id = _uniq("idem")
    first = ledger.credit(tenant_id, 5.0, idempotency_key="topup-1")
    second = ledger.credit(tenant_id, 5.0, idempotency_key="topup-2")

    assert first != second
    assert len(_ids(ledger, tenant_id)) == 2


@pytest.mark.pg
def test_credit_idempotent_key_is_tenant_scoped(ledger):
    """Same idempotency_key across tenants must yield distinct ids, one entry each."""
    tenant_a = _uniq("idem-a")
    tenant_b = _uniq("idem-b")
    id_a = ledger.credit(tenant_a, 5.0, idempotency_key="shared-key")
    id_b = ledger.credit(tenant_b, 5.0, idempotency_key="shared-key")

    assert id_a != id_b
    assert len(_ids(ledger, tenant_a)) == 1
    assert len(_ids(ledger, tenant_b)) == 1


@pytest.mark.pg
def test_top_up_idempotent_retry(monkeypatch):
    """Retry with the same Idempotency-Key must not double-credit."""
    import routers.billing as billing_router

    fresh = PGBillingLedger()
    monkeypatch.setattr(billing_router, "billing_ledger", fresh)

    import deps

    monkeypatch.setenv("ROMA_ADMIN_KEYS", "test-admin-key")
    monkeypatch.setattr(deps, "ADMIN_IP_ALLOWLIST", "*")

    import main

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)

    target = _uniq("idem-target")
    client = TestClient(main.app, raise_server_exceptions=False)
    headers = {"X-API-Key": "test-admin-key", "Idempotency-Key": "topup-abc"}
    body = {"tenant_id": target, "amount": 3.0}

    r1 = client.post("/billing/top-up", json=body, headers=headers)
    r2 = client.post("/billing/top-up", json=body, headers=headers)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["entry_id"] == r2.json()["entry_id"]
    assert len(_ids(fresh, target)) == 1
