"""F-001 regression: a CP refusal must never reach the success branch.

CloudPayments reports a failed payment as ``OperationType=Payment`` +
``Status=Declined`` (and a cancelled payment as ``OperationType=Payment`` +
``Status=Cancelled``). The operation type alone therefore cannot mean "paid":
only a settled status may credit/activate the tenant. A refusal must reach the
Fail branch in ``routers/webhooks.py`` instead.

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
    status="Declined",
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
    calls = {"sub": [], "mark": [], "inactive": []}
    fake = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec-test"),
        verify_webhook=lambda body, sig: True,
    )
    monkeypatch.setattr(main, "CLOUDPAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "cloudpayments_client", fake)
    monkeypatch.setattr(main.db, "get_tenant", lambda tid: {"id": tid})
    monkeypatch.setattr(
        main.db, "update_tenant_subscription", lambda *a, **k: calls["sub"].append(a)
    )
    monkeypatch.setattr(
        main.db, "mark_invoice_processed", lambda *a, **k: calls["mark"].append(a)
    )
    monkeypatch.setattr(
        main.db, "set_tenant_inactive", lambda *a, **k: calls["inactive"].append(a)
    )
    monkeypatch.setattr(
        main.db, "is_invoice_processed", lambda invoice_id, tenant_id="": False
    )
    return {"calls": calls}


def _post(client, payload):
    return client.post(
        "/webhooks/cloudpayments",
        content=json.dumps(payload),
        headers={"Content-HMAC": "sig"},
    )


def test_payment_declined_is_not_success(cp):
    """OperationType=Payment + Status=Declined must not activate the tenant."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-declined", status="Declined"))

    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["inactive"] == [("tenant-a",)]
    assert len(cp["calls"]["mark"]) == 1


def test_payment_cancelled_is_not_success(cp):
    """OperationType=Payment + Status=Cancelled must not activate the tenant."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-cancelled", status="Cancelled"))

    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["inactive"] == [("tenant-a",)]


def test_payment_completed_still_succeeds(cp):
    """Control: a settled Status=Completed keeps the success path intact."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-ok", status="Completed"))

    assert resp.status_code == 200
    assert len(cp["calls"]["sub"]) == 1
    assert cp["calls"]["sub"][0][0] == "tenant-a"
    assert cp["calls"]["inactive"] == []


def test_payment_authorized_still_succeeds(cp):
    """Control: CP holds funds with Status=Authorized — success path unchanged."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-auth", status="Authorized"))

    assert resp.status_code == 200
    assert len(cp["calls"]["sub"]) == 1
    assert cp["calls"]["inactive"] == []
