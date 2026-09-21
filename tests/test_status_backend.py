"""P0-1: GET /status/{job_id} exposes backend and backend_job_id.

Covers:
  - after POST /submit with an explicit backend, /status returns 200 and both
    keys (backend populated, backend_job_id still None before dispatch).
  - after dispatch persists backend + backend_job_id on the execution job,
    /status returns both values (no 500, no dropped keys).

Run with the project venv:
    /home/felix/dsh-workspace/.venv-roma/bin/python -m pytest tests/test_status_backend.py -q
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
    """Prevent the infinite poll_and_execute loop from running during tests."""
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def tenant(monkeypatch):
    tenant_id = _uniq("test-status")
    key = _uniq("key-status")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "status"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    return {"tenant_id": tenant_id, "key": key}


def test_status_after_submit_contains_backend_keys(tenant):
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = client.post(
        "/submit",
        json={"task": "echo hi", "backend": "vastai"},
        headers={"X-API-Key": tenant["key"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = client.get(f"/status/{job_id}", headers={"X-API-Key": tenant["key"]})
    assert status.status_code == 200

    body = status.json()
    assert "backend" in body
    assert "backend_job_id" in body
    assert body["backend"] == "vastai"
    # Dispatch hasn't run in the test, so no backend job id has been assigned yet.
    assert body["backend_job_id"] is None


def test_status_after_dispatch_exposes_backend_job_id(tenant):
    client = TestClient(main.app, raise_server_exceptions=False)

    job_id = _uniq("job")
    db.insert_execution_job(
        job_id=job_id,
        decision_id=_uniq("dec"),
        tenant_id=tenant["tenant_id"],
        status="running",
        payload={},
    )
    db.update_execution_job(
        job_id,
        backend="gpu_worker",
        backend_job_id="worker-contract-123",
    )

    status = client.get(f"/status/{job_id}", headers={"X-API-Key": tenant["key"]})
    assert status.status_code == 200

    body = status.json()
    assert body["backend"] == "gpu_worker"
    assert body["backend_job_id"] == "worker-contract-123"


def test_status_without_backend_still_200(tenant):
    """A job submitted without a backend must not 500; the keys are still present."""
    client = TestClient(main.app, raise_server_exceptions=False)

    resp = client.post(
        "/submit",
        json={"task": "echo hi"},
        headers={"X-API-Key": tenant["key"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    status = client.get(f"/status/{job_id}", headers={"X-API-Key": tenant["key"]})
    assert status.status_code == 200

    body = status.json()
    assert "backend" in body
    assert "backend_job_id" in body
