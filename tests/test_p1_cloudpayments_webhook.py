"""P1-12 CloudPayments webhook attribution + webhook-secret regression tests.

Mocks the CloudPayments client and db.* — no network, no real secrets.

Run with the project venv:
    /home/felix/dsh-workspace/.venv-roma/bin/python -m pytest tests/test_p1_cloudpayments_webhook.py -q
"""

import hashlib
import hmac
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import main


def _payload(account_id=None, invoice_id="inv-1", plan="pro",
             operation="Payment", status="Completed"):
    body = {
        "OperationType": operation,
        "Status": status,
        "InvoiceId": invoice_id,
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
    monkeypatch.setattr(main.db, "update_tenant_subscription",
                        lambda *a, **k: calls["sub"].append(a))
    monkeypatch.setattr(main.db, "mark_invoice_processed",
                        lambda *a, **k: calls["mark"].append(a))
    monkeypatch.setattr(main.db, "set_tenant_inactive",
                        lambda *a, **k: calls["inactive"].append(a))
    monkeypatch.setattr(main.db, "is_invoice_processed", lambda invoice_id: False)
    return {"calls": calls, "fake": fake}


def _post(client, payload, signature="sig"):
    return client.post(
        "/webhooks/cloudpayments",
        content=json.dumps(payload),
        headers={"Content-HMAC": signature},
    )


def test_invalid_signature_no_credit(cp, monkeypatch):
    monkeypatch.setattr(cp["fake"], "verify_webhook", lambda body, sig: False)
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(account_id="tenant-a", invoice_id="inv-bad"), "wrong")
    assert resp.status_code == 400
    assert cp["calls"]["sub"] == []
    assert cp["calls"]["mark"] == []


def test_valid_signature_credits_a_not_b(cp, monkeypatch):
    monkeypatch.setattr(cp["fake"], "verify_webhook", lambda body, sig: True)
    monkeypatch.setattr(main.db, "get_tenant",
                        lambda tid: {"id": tid} if tid == "tenant-a" else None)
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(account_id="tenant-a", invoice_id="inv-1"))
    assert resp.status_code == 200
    sub_calls = cp["calls"]["sub"]
    assert len(sub_calls) == 1
    assert sub_calls[0][0] == "tenant-a"
    assert all(c[0] != "tenant-b" for c in sub_calls)


def test_missing_or_unknown_accountid_no_credit(cp, monkeypatch):
    monkeypatch.setattr(cp["fake"], "verify_webhook", lambda body, sig: True)
    monkeypatch.setattr(main.db, "get_tenant", lambda tid: None)  # no known tenants
    client = TestClient(main.app, raise_server_exceptions=False)

    r1 = _post(client, _payload(account_id=None, invoice_id="inv-noid"))
    assert r1.status_code == 422

    r2 = _post(client, _payload(account_id="ghost", invoice_id="inv-ghost"))
    assert r2.status_code == 422

    assert cp["calls"]["sub"] == []


def test_repeat_invoice_no_second_credit(cp, monkeypatch):
    monkeypatch.setattr(cp["fake"], "verify_webhook", lambda body, sig: True)
    monkeypatch.setattr(main.db, "get_tenant", lambda tid: {"id": tid})
    client = TestClient(main.app, raise_server_exceptions=False)
    payload = _payload(account_id="tenant-a", invoice_id="inv-repeat")

    monkeypatch.setattr(main.db, "is_invoice_processed", lambda iid: False)
    r1 = _post(client, payload)
    assert r1.status_code == 200
    assert len(cp["calls"]["sub"]) == 1

    monkeypatch.setattr(main.db, "is_invoice_processed", lambda iid: True)
    r2 = _post(client, payload)
    assert r2.status_code == 200
    assert len(cp["calls"]["sub"]) == 1  # still exactly one credit


def test_webhook_secret_unset_fail_closed(cp, monkeypatch):
    cp["fake"].config.webhook_secret = ""
    monkeypatch.setattr(cp["fake"], "verify_webhook", lambda body, sig: True)
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = _post(client, _payload(account_id="tenant-a", invoice_id="inv-nosecret"))
    assert resp.status_code == 500
    assert cp["calls"]["sub"] == []


def test_verify_webhook_never_falls_back_to_api_secret():
    from billing.cloudpayments_client import CloudPaymentsConfig, CloudPaymentsClient
    cfg = CloudPaymentsConfig(public_id="p", api_secret="api-secret-xyz", webhook_secret="")
    client = CloudPaymentsClient(cfg)
    body = b"{}"
    # HMAC computed with the API secret must NOT be accepted.
    sig = hmac.new(b"api-secret-xyz", body, hashlib.sha256).hexdigest()
    assert client.verify_webhook(body, sig) is False


def test_create_order_requires_nonempty_account_id():
    from billing.cloudpayments_client import CloudPaymentsConfig, CloudPaymentsClient
    client = CloudPaymentsClient(
        CloudPaymentsConfig(public_id="p", api_secret="s", webhook_secret="wh")
    )
    calls = []
    client._post = lambda path, payload: calls.append((path, payload)) or {"Url": "https://example.com"}

    # Empty / whitespace account_id -> ValueError, no HTTP call.
    with pytest.raises(ValueError):
        client.create_order(amount=100, currency="RUB", description="d", account_id="")
    with pytest.raises(ValueError):
        client.create_order(amount=100, currency="RUB", description="d", account_id="   ")
    assert calls == []

    # Valid account_id -> single POST with non-empty normalized AccountId.
    client.create_order(amount=100, currency="RUB", description="d", account_id="  tenant-1  ")
    assert len(calls) == 1
    assert calls[0][1]["AccountId"] == "tenant-1"


def test_create_order_missing_account_id_is_typeerror():
    from billing.cloudpayments_client import CloudPaymentsConfig, CloudPaymentsClient
    client = CloudPaymentsClient(
        CloudPaymentsConfig(public_id="p", api_secret="s", webhook_secret="wh")
    )
    with pytest.raises(TypeError):
        client.create_order(amount=100, currency="RUB", description="d")

