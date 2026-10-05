"""F-005 regression: the InvoiceId dedup must be scoped to the resolved tenant.

Before the fix the idempotency check ran *before* tenant resolution and matched
an InvoiceId from any tenant, so a foreign (or unresolved) ``AccountId`` was
answered with a silent ``{"code": 0}`` and the real tenant's event was dropped.

Contract pinned here (``routers/webhooks.py``):
  * tenant resolution from ``AccountId`` happens BEFORE the dedup;
  * a repeat for the SAME tenant -> ``{"code": 0}`` (unchanged);
  * the same InvoiceId recorded for ANOTHER tenant -> 409, never ``{"code": 0}``;
  * an unknown/empty ``AccountId`` -> 422, never ``{"code": 0}``.

Mocks the CloudPayments client and db.* — no network, no real secrets.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import main


def _payload(
    account_id="tenant-a",
    invoice_id="inv-1",
    plan="pro",
    operation="Payment",
    status="Completed",
    amount=4900.00,
):
    body = {
        "OperationType": operation,
        "Status": status,
        "InvoiceId": invoice_id,
        "Amount": amount,
        "Data": {"plan": plan},
    }
    if account_id is not None:
        body["AccountId"] = account_id
    return body


@pytest.fixture()
def cp(monkeypatch):
    """Webhook wired to an in-memory processed_invoices store."""
    calls = {"sub": [], "mark": [], "inactive": []}
    store = {}  # invoice_id -> tenant_id that recorded it

    fake = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec-test"),
        verify_webhook=lambda body, sig: True,
    )
    monkeypatch.setattr(main, "CLOUDPAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "cloudpayments_client", fake)
    monkeypatch.setattr(
        main.db,
        "get_tenant",
        lambda tid: {"id": tid} if tid in ("tenant-a", "tenant-b") else None,
    )
    monkeypatch.setattr(
        main.db, "update_tenant_subscription", lambda *a, **k: calls["sub"].append(a)
    )
    monkeypatch.setattr(
        main.db, "mark_invoice_processed", lambda *a, **k: calls["mark"].append(a)
    )
    monkeypatch.setattr(
        main.db, "set_tenant_inactive", lambda *a, **k: calls["inactive"].append(a)
    )

    def _processed(invoice_id, tenant_id=""):
        if invoice_id not in store:
            return False
        if tenant_id:
            return store[invoice_id] == tenant_id
        return True

    monkeypatch.setattr(main.db, "is_invoice_processed", _processed)
    return {"calls": calls, "store": store}


def _post(client, payload):
    return client.post(
        "/webhooks/cloudpayments",
        content=json.dumps(payload),
        headers={"Content-HMAC": "sig"},
    )


def test_same_invoice_other_tenant_is_409_not_silent_zero(cp):
    """InvoiceId recorded for tenant-b must not silence tenant-a's event."""
    cp["store"]["inv-shared"] = "tenant-b"
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = _post(
        client,
        _payload(account_id="tenant-a", invoice_id="inv-shared", status="Completed"),
    )

    assert resp.status_code == 409
    assert resp.json().get("detail") == "Invoice belongs to another tenant"
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["inactive"] == []
    assert cp["calls"]["mark"] == []


def test_repeat_for_same_tenant_is_still_idempotent(cp):
    """A repeat of the SAME tenant keeps the silent {"code": 0} behaviour."""
    cp["store"]["inv-repeat"] = "tenant-a"
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = _post(
        client,
        _payload(account_id="tenant-a", invoice_id="inv-repeat", status="Completed"),
    )

    assert resp.status_code == 200
    assert resp.json() == {"code": 0}
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["mark"] == []


def test_unknown_account_id_is_422_not_silent_zero(cp):
    """An unresolved AccountId must be rejected, not answered with {"code": 0}."""
    cp["store"]["inv-orphan"] = "tenant-b"
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = _post(
        client,
        _payload(account_id="tenant-ghost", invoice_id="inv-orphan", status="Completed"),
    )

    assert resp.status_code == 422
    assert resp.json().get("detail") == "Unknown AccountId"
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["mark"] == []


def test_missing_account_id_is_422_not_silent_zero(cp):
    """No AccountId at all must never be answered with {"code": 0}."""
    cp["store"]["inv-noaccount"] = "tenant-b"
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = _post(
        client,
        _payload(account_id=None, invoice_id="inv-noaccount", status="Completed"),
    )

    assert resp.status_code == 422
    assert resp.json().get("detail") == "Unknown AccountId"


def test_fresh_invoice_for_resolved_tenant_still_credits(cp):
    """Control: a brand-new invoice for a known tenant keeps the success path."""
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = _post(
        client,
        _payload(account_id="tenant-a", invoice_id="inv-fresh", status="Completed"),
    )

    assert resp.status_code == 200
    assert resp.json() == {"code": 0}
    assert len(cp["calls"]["sub"]) == 1
    assert cp["calls"]["sub"][0][0] == "tenant-a"
    assert cp["calls"]["inactive"] == []
