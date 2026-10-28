"""Edge hardening: real client address, auth rate limits, Secure cookies, docs/metrics gate."""

from __future__ import annotations

import pytest
from starlette.requests import Request

import config
from deps import client_ip, limiter


def _request(peer: str, xff: str | None = None):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {"type": "http", "headers": headers, "method": "GET", "path": "/"}
    if peer is not None:
        scope["client"] = (peer, 12345)
    return Request(scope)


@pytest.fixture
def trust_proxy(monkeypatch):
    monkeypatch.setenv("ROMA_TRUST_PROXY", "1")


@pytest.fixture
def no_trust(monkeypatch):
    monkeypatch.delenv("ROMA_TRUST_PROXY", raising=False)


# ── client_ip ──────────────────────────────────────────────────────────


def test_rightmost_xff_entry_wins_behind_loopback_proxy(trust_proxy):
    assert client_ip(_request("127.0.0.1", "1.2.3.4, 203.0.113.7")) == "203.0.113.7"


def test_spoofed_left_entry_is_ignored(trust_proxy):
    assert client_ip(_request("127.0.0.1", "9.9.9.9, 203.0.113.7")) == "203.0.113.7"


def test_xff_ignored_without_trust_flag(no_trust):
    assert client_ip(_request("127.0.0.1", "203.0.113.7")) == "127.0.0.1"


def test_xff_ignored_for_non_loopback_peer(trust_proxy):
    assert client_ip(_request("203.0.113.5", "203.0.113.7")) == "203.0.113.5"


def test_loopback_proxy_without_xff_returns_peer(trust_proxy):
    assert client_ip(_request("127.0.0.1")) == "127.0.0.1"


# ── Secure cookie flag ─────────────────────────────────────────────────


def test_cookie_secure_true_for_public_https(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("ROMA_ENV", raising=False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://roma.example.com")
    assert config.cookie_secure() is True


def test_cookie_secure_false_for_local_dev(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("ROMA_ENV", raising=False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8900")
    assert config.cookie_secure() is False


def test_cookie_secure_false_for_loopback_https(monkeypatch):
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("ROMA_ENV", raising=False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://127.0.0.1:8900")
    assert config.cookie_secure() is False


def test_cookie_secure_true_in_production_env(monkeypatch):
    monkeypatch.setenv("ROMA_ENV", "production")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8900")
    assert config.cookie_secure() is True


# ── rate limits ────────────────────────────────────────────────────────


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    limiter.reset()
    # loopback peer so the limiter keys on a single stable address
    yield TestClient(main.app, client=("127.0.0.1", 12345))
    limiter.reset()


def test_signup_rate_limited_after_five(client):
    codes = [client.post("/auth/signup", json={}).status_code for _ in range(6)]
    assert codes[:5] == [400] * 5, codes
    assert codes[5] == 429, codes


def test_resend_verification_rate_limited_after_three(client):
    codes = [client.post("/auth/resend-verification", json={}).status_code for _ in range(4)]
    assert codes[:3] == [400] * 3, codes
    assert codes[3] == 429, codes


def test_verify_email_rate_limited_after_twenty(client):
    codes = [client.get("/auth/verify-email?token=nope").status_code for _ in range(21)]
    assert set(codes[:20]) == {400}, codes
    assert codes[20] == 429, codes


# ── /metrics and docs gate ─────────────────────────────────────────────


def test_metrics_served_over_loopback(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "roma_requests_total" in resp.text


def test_metrics_hidden_from_proxied_request(client):
    assert client.get("/metrics", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 404


def test_metrics_hidden_from_non_loopback_peer():
    from starlette.testclient import TestClient

    import main

    assert TestClient(main.app).get("/metrics").status_code == 404


def test_docs_hidden_from_proxied_request(client, monkeypatch):
    monkeypatch.delenv("ROMA_PUBLIC_DOCS", raising=False)
    assert client.get("/docs", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 404
    assert client.get("/openapi.json", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 404


def test_public_docs_flag_opens_docs_but_not_metrics(client, monkeypatch):
    monkeypatch.setenv("ROMA_PUBLIC_DOCS", "1")
    assert client.get("/docs", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200
    assert client.get("/metrics", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 404
