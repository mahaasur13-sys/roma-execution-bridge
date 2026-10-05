"""F-002 regression: the CloudPayments webhook signature contract is pinned and fail-closed.

Contract under test (billing/cloudpayments_client.py, verify_webhook):

* HMAC-SHA256 over the raw body, digest **hex**-encoded;
* the only accepted key is CLOUDPAYMENTS_WEBHOOK_SECRET;
* CLOUDPAYMENTS_API_SECRET is never accepted as a fallback;
* missing header / missing secret -> False.

No network: httpx.Client is replaced by a stub. The provider-side format itself is
UNVERIFIED in this repo, so these tests only pin *our* acceptance rule.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from billing import cloudpayments_client as cpc

BODY = b'{"InvoiceId":"inv-1","Status":"Completed","Amount":50.00}'
API_SECRET = "api-secret-value"
WEBHOOK_SECRET = "webhook-secret-value"


class _StubHttp:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.posts: list[tuple] = []

    def post(self, path, **kwargs):
        self.posts.append((path, kwargs))
        raise AssertionError("no HTTP call is allowed in this test")

    def close(self) -> None:
        return None


@pytest.fixture()
def client(monkeypatch) -> cpc.CloudPaymentsClient:
    monkeypatch.setattr(cpc.httpx, "Client", _StubHttp)
    return cpc.CloudPaymentsClient(
        cpc.CloudPaymentsConfig(
            public_id="pk-test",
            api_secret=API_SECRET,
            webhook_secret=WEBHOOK_SECRET,
            mode="test",
        )
    )


def _hex_sig(secret: str, body: bytes = BODY) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _b64_sig(secret: str, body: bytes = BODY) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def test_accepts_hex_hmac_of_webhook_secret(client):
    assert client.verify_webhook(BODY, _hex_sig(WEBHOOK_SECRET)) is True


def test_accepts_hex_with_surrounding_whitespace(client):
    assert client.verify_webhook(BODY, f"  {_hex_sig(WEBHOOK_SECRET)}\n") is True


def test_rejects_base64_digest_of_webhook_secret(client):
    assert client.verify_webhook(BODY, _b64_sig(WEBHOOK_SECRET)) is False


def test_rejects_hex_hmac_of_api_secret(client):
    """api_secret must never be an accepted signing key."""
    assert client.verify_webhook(BODY, _hex_sig(API_SECRET)) is False


def test_rejects_base64_hmac_of_api_secret(client):
    assert client.verify_webhook(BODY, _b64_sig(API_SECRET)) is False


def test_rejects_tampered_body(client):
    good = _hex_sig(WEBHOOK_SECRET)
    assert client.verify_webhook(BODY + b" ", good) is False


def test_rejects_missing_or_empty_header(client):
    assert client.verify_webhook(BODY, None) is False
    assert client.verify_webhook(BODY, "") is False
    assert client.verify_webhook(BODY, "   ") is False


def test_fail_closed_when_webhook_secret_absent(monkeypatch):
    """No CLOUDPAYMENTS_WEBHOOK_SECRET -> reject, even with a valid api_secret HMAC."""
    monkeypatch.setattr(cpc.httpx, "Client", _StubHttp)
    unconfigured = cpc.CloudPaymentsClient(
        cpc.CloudPaymentsConfig(public_id="pk-test", api_secret=API_SECRET)
    )
    assert unconfigured.config.webhook_secret == ""
    assert unconfigured.verify_webhook(BODY, _hex_sig(API_SECRET)) is False
    assert unconfigured.verify_webhook(BODY, _hex_sig("")) is False
