"""P1-11 idempotent submit regression tests.

Covers:
  - same tenant + same Idempotency-Key -> one job, replay returns same job_id/body
  - same key, different tenant -> different job_id
  - no header -> two distinct jobs
  - empty/whitespace Idempotency-Key -> 400

Run with the project venv:
    /home/felix/dsh-workspace/.venv-roma/bin/python -m pytest tests/test_p1_submit_idempotency.py -q
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import main


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def tenants(monkeypatch):
    a_id = _uniq("test-a")
    b_id = _uniq("test-b")
    key_a = _uniq("key-a")
    key_b = _uniq("key-b")
    db.seed_tenants(
        {
            key_a: {"tenant_id": a_id, "name": "A"},
            key_b: {"tenant_id": b_id, "name": "B"},
        }
    )
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    return {"a": a_id, "b": b_id, "key_a": key_a, "key_b": key_b}


def _submit(client, api_key, idem=None):
    headers = {"X-API-Key": api_key}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return client.post("/submit", json={"task": "echo hi"}, headers=headers)


def test_same_key_single_job_and_same_body(tenants):
    client = TestClient(main.app, raise_server_exceptions=False)

    r1 = _submit(client, tenants["key_a"], "idem-1")
    assert r1.status_code == 202
    jid1 = r1.json()["job_id"]

    r2 = _submit(client, tenants["key_a"], "idem-1")
    assert r2.status_code == 202
    assert r2.json()["job_id"] == jid1
    assert r2.json() == r1.json()

    # Only one execution job exists for the tenant.
    assert db.count_jobs_by_tenant(tenants["a"]) == 1


def test_same_key_different_tenant_different_job(tenants):
    client = TestClient(main.app, raise_server_exceptions=False)

    r1 = _submit(client, tenants["key_a"], "idem-shared")
    r2 = _submit(client, tenants["key_b"], "idem-shared")
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["job_id"] != r2.json()["job_id"]


def test_no_header_two_distinct_jobs(tenants):
    client = TestClient(main.app, raise_server_exceptions=False)

    r1 = _submit(client, tenants["key_a"])
    r2 = _submit(client, tenants["key_a"])
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert db.count_jobs_by_tenant(tenants["a"]) == 2


def test_empty_key_400(tenants):
    client = TestClient(main.app, raise_server_exceptions=False)

    r1 = _submit(client, tenants["key_a"], "   ")
    assert r1.status_code == 400
    r2 = _submit(client, tenants["key_a"], "")
    assert r2.status_code == 400
    # No job was created for the rejected requests.
    assert db.count_jobs_by_tenant(tenants["a"]) == 0


def test_key_too_long_400(tenants):
    client = TestClient(main.app, raise_server_exceptions=False)
    r = _submit(client, tenants["key_a"], "k" * 257)
    assert r.status_code == 400
