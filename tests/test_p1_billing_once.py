"""P1 billing regression tests: single debit + dead db.* calls.

Covers:
  - /submit does NOT debit (only queues).
  - /complete/{job_id} debits exactly once and is idempotent on repeat.
  - /stats/daily and the worker db.* helpers no longer raise AttributeError.

Run with the project venv:
    /home/felix/dsh-workspace/.venv-roma/bin/python -m pytest tests/test_p1_billing_once.py -q
"""

import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import main

# G-CI-PG-CANON: класс env-скипа ставится ОДНИМ механизмом — маркером `pg` в conftest
# (отсутствие PG_DSN → env-skip с тройкой issue/expiry), а не локальным skipif:
# два разных порога на одно явление — источник дефекта (скип уходил в класс admission
# и «зелёный» CI не отличал неисполненный money-path от осознанного скипа).


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    """Prevent startup from launching the infinite poll_and_execute loop."""

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def tenant(monkeypatch):
    """Seed one tenant with a known API key and bypass email verification."""
    tenant_id = _uniq("test-bill")
    key = _uniq("key-bill")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "A"}})
    main.billing_ledger.credit(tenant_id, 1.0)
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    return {"tenant_id": tenant_id, "key": key}


def _count_debits(tenant_id: str, job_id: str) -> int:
    """Count DEBIT ledger entries attributed to a specific job_id."""
    import json

    n = 0
    for entry in main.billing_ledger.get_tenant_entries(tenant_id):
        meta = entry.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        if entry.get("type") == "DEBIT" and meta.get("job_id") == job_id:
            n += 1
    return n


def _seed_running_job(tenant_id: str) -> str:
    """Insert a job and move it to running with started_at set in the past."""
    jid = _uniq("job")
    db.insert_execution_job(
        job_id=jid,
        decision_id=_uniq("dec"),
        tenant_id=tenant_id,
        status="queued",
        payload={},
    )
    db.update_execution_job(jid, status="running")  # sets started_at = now()
    time.sleep(1.1)  # ensure actual duration > 0 so a debit is applied
    return jid


def test_submit_does_not_debit(tenant):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.post(
        "/submit",
        json={"task": "echo hello"},
        headers={"X-API-Key": tenant["key"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert _count_debits(tenant["tenant_id"], job_id) == 0


@pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG
def test_complete_debits_exactly_once(tenant):
    job_id = _seed_running_job(tenant["tenant_id"])
    client = TestClient(main.app, raise_server_exceptions=False)

    r1 = client.post(f"/complete/{job_id}", headers={"X-API-Key": tenant["key"]})
    assert r1.status_code == 200
    assert _count_debits(tenant["tenant_id"], job_id) == 1

    # Repeated complete must not debit a second time.
    r2 = client.post(f"/complete/{job_id}", headers={"X-API-Key": tenant["key"]})
    assert r2.status_code == 200
    assert _count_debits(tenant["tenant_id"], job_id) == 1


def test_stats_daily_smoke(tenant):
    client = TestClient(main.app, raise_server_exceptions=False)
    resp = client.get("/stats/daily")
    assert resp.status_code == 200
    data = resp.json()
    assert "dates" in data and "jobs_count" in data and "gpu_hours" in data


def test_worker_db_functions_exist(tenant):
    # These are invoked by /ws/worker — they must be defined and not raise
    # AttributeError. (Full websocket handshake is exercised separately.)
    db.register_worker("w-bill-1", tenant["tenant_id"], {"gpu": 1})
    db.update_worker_heartbeat("w-bill-1")
    db.release_worker("w-bill-1")


@pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG
def test_complete_without_funds_returns_402(monkeypatch):
    """fail-closed: no CREDIT → /complete 402, no debit, status unchanged."""
    tenant_id = _uniq("test-bill")
    key = _uniq("key-bill")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "A"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    job_id = _seed_running_job(tenant_id)
    client = TestClient(main.app, raise_server_exceptions=False)
    r1 = client.post(f"/complete/{job_id}", headers={"X-API-Key": key})
    assert r1.status_code == 402
    assert _count_debits(tenant_id, job_id) == 0
    job = db.get_execution_job(job_id)
    assert job["status"] == "running"
