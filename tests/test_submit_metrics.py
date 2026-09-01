"""B5/D2: /submit increments roma_jobs_total exactly once (no double-count)."""

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
def tenant(monkeypatch):
    tenant_id = _uniq("t-metrics")
    key = _uniq("key-metrics")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "A"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    return {"tenant_id": tenant_id, "key": key}


def _jobs_total(tenant_id: str) -> float:
    return main.roma_jobs_total.labels(tenant_id=tenant_id)._value.get()


def test_submit_increments_jobs_total_once(tenant):
    client = TestClient(main.app, raise_server_exceptions=False)
    before = _jobs_total(tenant["tenant_id"])

    resp = client.post(
        "/submit",
        json={"task": "echo hi"},
        headers={"X-API-Key": tenant["key"]},
    )
    assert resp.status_code == 202

    after = _jobs_total(tenant["tenant_id"])
    assert after - before == 1  # not 2 (no duplicate increment)
