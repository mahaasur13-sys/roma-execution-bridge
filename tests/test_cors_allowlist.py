"""Tests for CORS allowlist from CORS_ALLOW_ORIGINS (C5).

Each scenario builds its own short ``FastAPI() + CORSMiddleware`` app with the
origins/credentials computed by the same pure functions ``main`` uses, under an
isolated monkeypatched env — never relying on the import-time ``main.app``.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

import main


def _build_cors_app() -> tuple[FastAPI, list[str], bool]:
    """Build a minimal app with CORS configured exactly like main's pure helpers."""
    origins = main._cors_allowed_origins()
    credentials = main._cors_allow_credentials(origins)
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app, origins, credentials


def _non_prod(monkeypatch, cors: str):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", cors)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("ROMA_ENV", raising=False)


def test_explicit_origins_trimmed(monkeypatch):
    _non_prod(monkeypatch, cors="http://a.example, http://b.example, ")
    assert main._cors_allowed_origins() == ["http://a.example", "http://b.example"]


def test_nonprod_empty_localhost_default(monkeypatch):
    _non_prod(monkeypatch, cors="")
    assert main._cors_allowed_origins() == ["http://127.0.0.1:3080", "http://localhost:3080"]


def test_allowed_origin_reflected(monkeypatch):
    _non_prod(monkeypatch, cors="http://localhost:3080")
    app, origins, credentials = _build_cors_app()
    assert origins == ["http://localhost:3080"]
    assert credentials is True

    client = TestClient(app)
    resp = client.get("/health", headers={"Origin": "http://localhost:3080"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3080"
    assert resp.headers.get("access-control-allow-credentials") == "true"


def test_foreign_origin_not_reflected(monkeypatch):
    _non_prod(monkeypatch, cors="http://localhost:3080")
    app, _, _ = _build_cors_app()

    client = TestClient(app)
    resp = client.get("/health", headers={"Origin": "http://evil.example"})
    assert resp.headers.get("access-control-allow-origin") != "http://evil.example"


def test_production_empty_fail_closed(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "")
    monkeypatch.delenv("ROMA_ENV", raising=False)
    app, origins, _ = _build_cors_app()

    assert origins == []
    assert "*" not in origins

    client = TestClient(app)
    resp = client.get("/health", headers={"Origin": "http://localhost:3080"})
    assert resp.headers.get("access-control-allow-origin") != "http://localhost:3080"


def test_wildcard_disables_credentials(monkeypatch):
    _non_prod(monkeypatch, cors="*")
    app, origins, credentials = _build_cors_app()

    assert origins == ["*"]
    assert credentials is False  # _cors_allow_credentials(["*"]) is False

    client = TestClient(app)
    resp = client.get("/health", headers={"Origin": "http://anything.example"})
    assert resp.headers.get("access-control-allow-credentials") != "true"
