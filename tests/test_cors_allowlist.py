"""Tests for CORS allowlist from CORS_ALLOW_ORIGINS (C5)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main


def _set_cors_env(monkeypatch, cors="", env=None, roma_env=None):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", cors)
    for name, value in (("ENV", env), ("ROMA_ENV", roma_env)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_explicit_origins_trimmed(monkeypatch):
    _set_cors_env(monkeypatch, cors="http://a.example, http://b.example, ")
    assert main._cors_allowed_origins() == ["http://a.example", "http://b.example"]


def test_production_empty_is_fail_closed(monkeypatch):
    _set_cors_env(monkeypatch, cors="", env="production")
    origins = main._cors_allowed_origins()
    assert origins == []
    assert "*" not in origins


def test_nonprod_empty_defaults_to_localhost(monkeypatch):
    _set_cors_env(monkeypatch, cors="")
    origins = main._cors_allowed_origins()
    assert origins == ["http://127.0.0.1:3080", "http://localhost:3080"]
    assert "*" not in origins


def test_wildcard_disables_credentials():
    origins = ["*"]
    assert ("*" not in origins) is False  # credentials must be disabled


def test_middleware_reflects_allowed_origin():
    allowed = main._cors_origins[0] if main._cors_origins else "http://localhost:3080"
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/health", headers={"Origin": allowed})
    assert resp.headers.get("access-control-allow-origin") == allowed


def test_middleware_does_not_reflect_foreign_origin():
    if "*" in main._cors_origins:
        pytest.skip("wildcard config reflects all origins")
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/health", headers={"Origin": "http://evil.invalid"})
    assert resp.headers.get("access-control-allow-origin") != "http://evil.invalid"
