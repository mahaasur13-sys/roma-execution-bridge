"""F-004 regression: the webhook Amount must match the plan price before credit.

A CloudPayments notification that reports an Amount different from the plan price
must never activate a tenant or be recorded as a successful payment (fail closed:
a missing or unparsable Amount is a mismatch too).

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
        "Data": {"plan": plan},
    }
    if amount is not _MISSING:
        body["Amount"] = amount
    if account_id is not None:
        body["AccountId"] = account_id
    return body


_MISSING = object()


@pytest.fixture()
def cp(monkeypatch):
    calls = {"sub": [], "mark": [], "inactive": []}
    fake = SimpleNamespace(
        config=SimpleNamespace(webhook_secret="whsec-test"),
        verify_webhook=lambda body, sig: True,
    )
    monkeypatch.setattr(main, "CLOUDPAYMENTS_ENABLED", True)
    monkeypatch.setattr(main, "cloudpayments_client", fake)
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
    monkeypatch.setattr(main.db, "get_tenant", lambda tid: {"id": tid})
    return {"calls": calls, "fake": fake}


def _post(client, payload, signature="sig"):
    return client.post(
        "/webhooks/cloudpayments",
        content=json.dumps(payload),
        headers={"Content-HMAC": signature},
    )


def test_amount_matching_plan_credits(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-ok", amount=4900.00))
    assert resp.status_code == 200
    assert resp.json() == {"code": 0}
    assert len(cp["calls"]["sub"]) == 1
    assert cp["calls"]["sub"][0][0] == "tenant-a"
    assert cp["calls"]["mark"][0][1] == "Payment"


def test_amount_mismatch_does_not_credit(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-cheap", amount=1.00))
    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["inactive"] == []
    assert [c[1] for c in cp["calls"]["mark"]] == ["amount_mismatch"]


def test_overpaid_amount_does_not_credit(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-rich", amount=490000.00))
    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []


def test_amount_of_other_plan_does_not_credit(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(
        client, _payload(invoice_id="inv-cross", plan="pro", amount=29900.00)
    )
    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []


def test_enterprise_amount_credits_enterprise(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(
        client,
        _payload(invoice_id="inv-ent", plan="enterprise", amount=29900.00),
    )
    assert resp.status_code == 200
    assert len(cp["calls"]["sub"]) == 1
    assert cp["calls"]["sub"][0][4] == "enterprise"


def test_missing_amount_fails_closed(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-noamount", amount=_MISSING))
    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []
    assert [c[1] for c in cp["calls"]["mark"]] == ["amount_mismatch"]


def test_unparsable_amount_fails_closed(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    for bad in ("", "abc", None, [], {}):
        cp["calls"]["sub"].clear()
        resp = _post(client, _payload(invoice_id=f"inv-bad-{bad}", amount=bad))
        assert resp.status_code == 200
        assert cp["calls"]["sub"] == [], bad


def test_amount_string_number_credits(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-str", amount="4900.00"))
    assert resp.status_code == 200
    assert len(cp["calls"]["sub"]) == 1


def test_amount_within_tolerance_credits(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-eps", amount=4900.005))
    assert resp.status_code == 200
    assert len(cp["calls"]["sub"]) == 1


def test_amount_just_outside_tolerance_does_not_credit(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(invoice_id="inv-off", amount=4900.05))
    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []


def test_recurrent_amount_mismatch_does_not_credit(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(
        client,
        _payload(
            invoice_id="inv-rec",
            operation="Recurrent",
            status="Completed",
            amount=10.00,
        ),
    )
    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []
    assert [c[1] for c in cp["calls"]["mark"]] == ["amount_mismatch"]


def test_recurrent_amount_match_credits(cp):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(
        client,
        _payload(
            invoice_id="inv-rec-ok",
            operation="Recurrent",
            status="Completed",
            amount=4900.00,
        ),
    )
    assert resp.status_code == 200
    assert len(cp["calls"]["sub"]) == 1


def test_refusal_still_goes_to_fail_branch_not_amount_gate(cp):
    """A declined payment is a refusal, not an amount mismatch (F-001 intact)."""
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(
        client,
        _payload(invoice_id="inv-decl", status="Declined", amount=1.00),
    )
    assert resp.status_code == 200
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["inactive"] == [("tenant-a",)]
