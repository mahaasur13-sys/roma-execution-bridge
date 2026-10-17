"""Fail-closed + dedup regression for the live Stripe webhook microservice.

Loads ``deploy/stripe-webhook/app/main.py`` directly (it is not a package) and
stubs ``prometheus_fastapi_instrumentator``, which is not installed in the dev
environment. No network, no real Redis: the module global ``_redis`` is
monkeypatched per test.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import sys
import types
from pathlib import Path

from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_PATH = REPO_ROOT / "deploy" / "stripe-webhook" / "app" / "main.py"

# Stub the optional instrumentation dependency before loading the app module.
if "prometheus_fastapi_instrumentator" not in sys.modules:
    _stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app):
            return self

        def expose(self, app):
            return self

    _stub.Instrumentator = lambda: _Instrumentator()
    sys.modules["prometheus_fastapi_instrumentator"] = _stub

_spec = importlib.util.spec_from_file_location("stripe_webhook_app", APP_PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

SECRET = "whsec_test"


def _stripe_sig(secret: str, payload: bytes, t: str = "1234567890") -> str:
    signed = f"{t}.{payload.decode()}".encode()
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def test_verify_fail_closed_no_sig():
    assert mod._verify(b"{}", "", SECRET) is False


def test_verify_fail_closed_no_secret():
    assert mod._verify(b"{}", "t=1,v1=x", "") is False


def test_verify_rejects_wrong_signature():
    assert mod._verify(b"{}", "t=1,v1=deadbeef", SECRET) is False


def test_verify_accepts_valid_signature():
    body = b'{"id":"evt_1"}'
    assert mod._verify(body, _stripe_sig(SECRET, body), SECRET) is True


def test_no_secret_returns_503(monkeypatch):
    monkeypatch.setattr(mod, "STRIPE_SECRET", "")
    client = TestClient(mod.app, raise_server_exceptions=False)
    resp = client.post(
        "/webhook/stripe", content="{}",
        headers={"Stripe-Signature": "t=1,v1=x"},
    )
    assert resp.status_code == 503


def test_missing_signature_returns_400(monkeypatch):
    monkeypatch.setattr(mod, "STRIPE_SECRET", SECRET)
    monkeypatch.setattr(mod, "_redis", None)
    client = TestClient(mod.app, raise_server_exceptions=False)
    resp = client.post("/webhook/stripe", content="{}")
    assert resp.status_code == 400


def test_wrong_signature_returns_400(monkeypatch):
    monkeypatch.setattr(mod, "STRIPE_SECRET", SECRET)
    monkeypatch.setattr(mod, "_redis", None)
    client = TestClient(mod.app, raise_server_exceptions=False)
    resp = client.post(
        "/webhook/stripe", content="{}",
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 400


class _FakeRedis:
    def __init__(self):
        self.keys = set()
        self.enqueued = 0

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def xadd(self, *args, **kwargs):
        self.enqueued += 1
        return "ok"


def test_duplicate_event_processed_once(monkeypatch):
    monkeypatch.setattr(mod, "STRIPE_SECRET", SECRET)
    fake = _FakeRedis()
    monkeypatch.setattr(mod, "_redis", fake)

    async def _noop(tenant_id, event_type):
        return None

    monkeypatch.setattr(mod, "_sync_tenant", _noop)

    body = (
        b'{"id":"evt_dup","type":"invoice.paid",'
        b'"data":{"object":{"metadata":{"tenant_id":"t-pro"}}}}'
    )
    sig = _stripe_sig(SECRET, body)

    client = TestClient(mod.app, raise_server_exceptions=False)
    r1 = client.post("/webhook/stripe", content=body,
                     headers={"Stripe-Signature": sig})
    r2 = client.post("/webhook/stripe", content=body,
                     headers={"Stripe-Signature": sig})

    assert r1.status_code == 200
    assert r1.json()["processed"] is True
    assert r2.status_code == 200
    assert r2.json()["processed"] is False
    assert fake.enqueued == 1
