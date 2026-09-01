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

    def record_usage_event(self, *args, **kwargs):
        pass


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


def test_payload_backend_not_used_when_result_has_no_backend():
    """CodeRabbit follow-up: only result["backend"] decides success/failure.

    payload.backend is the *requested* backend and must not be persisted as the
    actual backend, nor keep a failed dispatch alive as "running".
    """
    result, db, status_calls = _run(
        {"status": "failed", "message": "dispatch failed"},
        payload={"backend": "vastai"},
    )

    assert result["status"] == "failed"
    assert status_calls == []  # no 120s poll loop
    assert len(db.updates) == 1
    _, kwargs = db.updates[0]
    assert kwargs["status"] == "failed"
    assert kwargs["backend"] is None  # payload.backend="vastai" must NOT be used


def test_queued_without_backend_fails_fast():
    result, db, status_calls = _run({"status": "queued"})

    assert result["status"] == "failed"
    assert status_calls == []  # no poll loop
    assert len(db.updates) == 1
    _, kwargs = db.updates[0]
    assert kwargs["status"] == "failed"
    assert kwargs["backend"] is None


def test_result_backend_overwrites_payload_backend(monkeypatch):
    """After successful dispatch, payload["backend"] must reflect the actually
    dispatched backend, so the poll loop / _execute_command sees gpu_worker,
    not the client-requested vastai."""
    import main as main_module

    db = _FakeDB()
    status_calls = []

    async def fake_dispatch(job_id, tenant_id, payload):
        return {"status": "running", "backend": "gpu_worker", "worker_id": "w1"}

    async def fake_status(job_id, instance_id=None):
        status_calls.append((job_id, instance_id))
        # Terminal status → poll loop exits on the first iteration.
        return {"status": "completed", "job_id": job_id}

    ew._db_adapter = db
    ew._backend_manager = {
        "dispatch": fake_dispatch,
        "cancel": _fake_cancel,
        "status": fake_status,
    }
    monkeypatch.setattr(main_module, "finalize_job_billing", lambda *a, **k: True)

    payload = {"backend": "vastai", "task": "echo hi"}
    result = asyncio.run(ew.execute_and_bill("job-gw", "tenant-1", payload))

    # The client-requested backend was overwritten with the dispatched backend.
    assert payload["backend"] == "gpu_worker"
    assert result["backend"] == "gpu_worker"
    assert status_calls  # dispatch succeeded, so the poll loop did run


def test_queued_with_backend_not_ready_fails_fast():
    """dispatch returned a backend name but status=queued is not "accepted" —
    don't mark running and don't burn 120s polling."""
    result, db, status_calls = _run({"status": "queued", "backend": "local"})

    assert result["status"] == "failed"
    assert status_calls == []  # no poll loop
    assert len(db.updates) == 1
    _, kwargs = db.updates[0]
    assert kwargs["status"] == "failed"
    assert kwargs["backend"] == "local"  # honest backend name, but still failed


def test_poll_uses_normalized_backend_job_id(monkeypatch):
    """poll must receive the normalized backend_job_id, not the raw contract_id."""
    import main as main_module

    db = _FakeDB()
    status_calls = []

    async def fake_dispatch(job_id, tenant_id, payload):
        return {
            "status": "running",
            "backend": "gpu_worker",
            "contract_id": 123,
            "backend_job_id": "worker-contract-xyz",
        }

    async def fake_status(job_id, instance_id=None):
        status_calls.append((job_id, instance_id))
        return {"status": "completed", "job_id": job_id}

    ew._db_adapter = db
    ew._backend_manager = {
        "dispatch": fake_dispatch,
        "cancel": _fake_cancel,
        "status": fake_status,
    }
    monkeypatch.setattr(main_module, "finalize_job_billing", lambda *a, **k: True)

    payload = {"backend": "vastai", "task": "echo hi"}
    asyncio.run(ew.execute_and_bill("job-gw", "tenant-1", payload))

    # payload carried the normalized backend_job_id, not str(contract_id).
    assert payload["backend_job_id"] == "worker-contract-xyz"
    # The poll loop used the normalized backend_job_id as instance_id.
    assert status_calls and status_calls[0][1] == "worker-contract-xyz"
