"""Admin API keys come from ROMA_ADMIN_KEYS only — and nothing else.

`deps.verify_api_key` used to carry two hardcoded literals, so a key that was
never configured anywhere still skipped the email-verification gate. Both
verifiers (``deps`` and the ``main`` copy) now read one helper,
``deps.admin_keys()``, which is fail-closed: no env value means no admins.
"""

import os
import sys
import uuid

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import deps
import main

LEGACY_KEYS = ("admin-key-beta-2026", "admin-super-key-xyz")


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def isolated(monkeypatch):
    """SQLite (session temp file from conftest), PG off, every tenant email unverified."""
    import db_adapter as db_adapter

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db_adapter, "_USE_PG", False)
    db.init_db()
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: False)
    return db_adapter


@pytest.fixture()
def tenant_key(isolated):
    key = _uniq("key-unverified")
    tenant_id = _uniq("tenant-plain")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "plain"}})
    return key, tenant_id


def test_admin_keys_empty_env_fail_closed(isolated, monkeypatch):
    monkeypatch.delenv("ROMA_ADMIN_KEYS", raising=False)
    assert deps.admin_keys() == set()


def test_admin_keys_parses_and_strips(monkeypatch):
    monkeypatch.setenv("ROMA_ADMIN_KEYS", " a ,, b ,")
    assert deps.admin_keys() == {"a", "b"}


def test_unverified_tenant_denied_without_env(tenant_key, monkeypatch):
    key, _tenant_id = tenant_key
    monkeypatch.delenv("ROMA_ADMIN_KEYS", raising=False)

    with pytest.raises(HTTPException) as exc:
        deps.verify_api_key(key)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc_main:
        main.verify_api_key(key)
    assert exc_main.value.status_code == 403


def test_admin_key_from_env_skips_verification(tenant_key, monkeypatch):
    key, tenant_id = tenant_key
    monkeypatch.setenv("ROMA_ADMIN_KEYS", key)

    assert deps.verify_api_key(key)["tenant_id"] == tenant_id
    assert main.verify_api_key(key)["tenant_id"] == tenant_id


def test_former_hardcoded_keys_are_rejected(isolated, monkeypatch):
    """The old literals are neither admin nor a tenant — plain 401."""
    monkeypatch.delenv("ROMA_ADMIN_KEYS", raising=False)
    assert not (set(LEGACY_KEYS) & deps.admin_keys())

    for legacy in LEGACY_KEYS:
        with pytest.raises(HTTPException) as exc:
            deps.verify_api_key(legacy)
        assert exc.value.status_code == 401

        with pytest.raises(HTTPException) as exc_main:
            main.verify_api_key(legacy)
        assert exc_main.value.status_code == 401


def test_legacy_key_no_longer_skips_verification(isolated, monkeypatch):
    """A live tenant whose key is a former literal no longer bypasses verification.

    On the old code this returned the tenant (email gate skipped) because the
    literal was a hardcoded admin key. It must now be treated as any other key.
    """
    legacy = LEGACY_KEYS[0]
    tenant_id = _uniq("tenant-legacy")
    db.seed_tenants({legacy: {"tenant_id": tenant_id, "name": "legacy"}})
    monkeypatch.delenv("ROMA_ADMIN_KEYS", raising=False)

    with pytest.raises(HTTPException) as exc:
        deps.verify_api_key(legacy)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc_main:
        main.verify_api_key(legacy)
    assert exc_main.value.status_code == 403
