"""F1: e2e dispatch smoke — submit → status → fake dispatch (no network).

Narrow smoke: POST /submit creates a job, GET /status shows it to its tenant,
and an in-process fake dispatch runs it to completion without any socket to
vast.ai / runpod / real GPU. Does not spin up a real worker or orchestrate a
new dispatch path.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import billing.execution_worker as ew
import db_adapter as db
import main


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    """Prevent the infinite poll_and_execute loop from running in tests."""

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def tenant(monkeypatch):
    tenant_id = _uniq("test-e2e")
    key = _uniq("key-e2e")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "A"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    return {"tenant_id": tenant_id, "key": key}


# In-process fakes — no vast.ai / runpod / HTTP provider is ever contacted.
async def _fake_dispatch(job_id, tenant_id, payload):
    return {
        "status": "running",
        "backend": "local",
        "backend_job_id": "fake-1",
        "price_per_hour": 0.0,
    }


async def _fake_status(job_id, instance_id=None):
    return {"status": "completed", "job_id": job_id}


async def _fake_cancel(job_id, tenant_id=""):
    return {"status": "cancelled", "job_id": job_id}


def test_submit_status_and_fake_dispatch(tenant, monkeypatch):
    client = TestClient(main.app, raise_server_exceptions=False)

    # 1. Submit (default local backend — no network to providers).
    resp = client.post(
        "/submit",
        json={"task": "echo hello"},
        headers={"X-API-Key": tenant["key"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # 2. Status visible to this tenant, still queued.
    resp = client.get(f"/status/{job_id}", headers={"X-API-Key": tenant["key"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] == "queued"

    # 3. In-process fake dispatch (no vast/runpod socket) → completed.
    ew._db_adapter = db
    ew._backend_manager = {
        "dispatch": _fake_dispatch,
        "cancel": _fake_cancel,
        "status": _fake_status,
    }
    monkeypatch.setattr(main, "finalize_job_billing", lambda *a, **k: "ok")

    result = asyncio.run(
        ew.execute_and_bill(job_id, tenant["tenant_id"], {"task": "echo hello"})
    )
    assert result["status"] == "completed"

    # 4. Status reflects the completed dispatch.
    resp = client.get(f"/status/{job_id}", headers={"X-API-Key": tenant["key"]})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


def test_status_is_tenant_scoped(tenant):
    client = TestClient(main.app, raise_server_exceptions=False)
    other_key = _uniq("key-other")
    db.seed_tenants({other_key: {"tenant_id": _uniq("test-other"), "name": "B"}})

    resp = client.post(
        "/submit", json={"task": "echo hi"}, headers={"X-API-Key": tenant["key"]}
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # A different tenant must not see this job.
    resp = client.get(f"/status/{job_id}", headers={"X-API-Key": other_key})
    assert resp.status_code == 404
