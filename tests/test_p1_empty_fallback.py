"""P1-1: execute_and_bill fails fast when there is nothing to execute.

Covers:
  - dispatch returns status="failed" (empty Vast.ai fallback chain) → job failed
    immediately, backend stays honest (never fabricated "local"), no 120s poll.
  - dispatch returns status="error" → job failed, no poll.
  - dispatch returns an empty result (no backend) → job failed, no poll.

Run with the project venv:
    /home/felix/dsh-workspace/.venv-roma/bin/python -m pytest tests/test_p1_empty_fallback.py -q
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import billing.execution_worker as ew


class _FakeDB:
    """Captures update_execution_job calls so we can assert the exact backend arg."""

    def __init__(self):
        self.updates = []

    def update_execution_job(self, job_id, **kwargs):
        self.updates.append((job_id, kwargs))


async def _fake_cancel(job_id, tenant_id=""):
    return {"status": "cancelled", "job_id": job_id}


def _run(dispatch_result, payload=None):
    """Wire fake dispatch/status/cancel, run execute_and_bill, return captures."""
    db = _FakeDB()
    status_calls = []

    async def fake_dispatch(job_id, tenant_id, payload):
        return dispatch_result

    async def fake_status(job_id, instance_id=None):
        status_calls.append((job_id, instance_id))
        return {"status": "running", "job_id": job_id}

    ew._db_adapter = db
    ew._backend_manager = {
        "dispatch": fake_dispatch,
        "cancel": _fake_cancel,
        "status": fake_status,
    }

    result = asyncio.run(ew.execute_and_bill("job-1", "tenant-1", payload or {}))
    return result, db, status_calls


def test_empty_fallback_chain_fails_fast():
    result, db, status_calls = _run({
        "status": "failed",
        "backend": "vastai",
        "message": "Failed to rent after exhausting chain ['RTX_4090', 'RTX_4080', 'RTX_3090']",
    })

    assert result["status"] == "failed"
    # No poll loop was entered (would otherwise wait up to 120s).
    assert status_calls == []
    # Exactly one state write: the job went straight to failed, not running.
    assert len(db.updates) == 1
    _, kwargs = db.updates[0]
    assert kwargs["status"] == "failed"
    assert kwargs["backend"] == "vastai"  # honest, not fabricated "local"


def test_dispatch_error_fails_fast():
    result, db, status_calls = _run({
        "status": "error",
        "message": "Vast.ai not configured: missing VAST_KEY",
    })

    assert result["status"] == "failed"
    assert status_calls == []
    assert len(db.updates) == 1
    _, kwargs = db.updates[0]
    assert kwargs["status"] == "failed"
    assert kwargs["backend"] is None  # no silent "local"


def test_empty_result_fails_fast():
    result, db, status_calls = _run({})

    assert result["status"] == "failed"
    assert status_calls == []
    assert len(db.updates) == 1
    _, kwargs = db.updates[0]
    assert kwargs["status"] == "failed"
    assert kwargs["backend"] is None  # no silent "local"
