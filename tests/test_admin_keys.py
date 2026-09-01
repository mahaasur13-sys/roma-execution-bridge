"""C2: admin API keys are sourced from ADMIN_API_KEYS env (fail-closed), not hardcoded."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

import db_adapter as db
import main


def test_admin_api_keys_empty_by_default(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEYS", raising=False)
    assert main._admin_api_keys() == set()


def test_admin_api_keys_from_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEYS", " key-one,key-two ,")
    assert main._admin_api_keys() == {"key-one", "key-two"}


def test_demo_api_key_from_env(monkeypatch):
    monkeypatch.setenv("ROMA_DEMO_API_KEY", "demo-123")
    assert main._demo_api_key() == "demo-123"
    monkeypatch.delenv("ROMA_DEMO_API_KEY", raising=False)
    assert main._demo_api_key() == ""


def test_admin_key_bypasses_email_verification(monkeypatch):
    """Empty ADMIN_API_KEYS → email verification enforced (403); key present in
    env → bypasses email verification."""
    key = f"admin-env-{uuid.uuid4().hex[:8]}"
    tenant_id = f"t-admin-{uuid.uuid4().hex[:8]}"
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "A"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: False)

    # Empty env → fail-closed: email verification enforced → 403.
    monkeypatch.delenv("ADMIN_API_KEYS", raising=False)
    with pytest.raises(HTTPException) as exc:
        main.verify_api_key(key)
    assert exc.value.status_code == 403

    # Key in ADMIN_API_KEYS → bypasses email verification.
    monkeypatch.setenv("ADMIN_API_KEYS", key)
    info = main.verify_api_key(key)
    assert info["tenant_id"] == tenant_id
