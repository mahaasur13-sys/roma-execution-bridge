import asyncio
import pytest
from billing import execution_worker as ew

@pytest.mark.asyncio
async def test_cancelled_execute_and_bill_sets_terminal_and_cleans(monkeypatch):
    updated, cleaned = [], []

    class FakeDB:
        def update_execution_job(self, *a, **k):
            updated.append((a, k))

    async def fake_dispatch(**k):
        await asyncio.sleep(10)  # зависнет — будет отменён

    async def fake_cancel(job_id, tenant_id):
        cleaned.append((job_id, tenant_id))

    monkeypatch.setattr(ew, "_db_adapter", FakeDB())
    monkeypatch.setattr(ew, "_backend_manager", {"dispatch": fake_dispatch, "cancel": fake_cancel, "status": lambda *a, **k: {"status": "running"}})

    task = asyncio.create_task(ew.execute_and_bill("job-test", "tenant-test", {"task": "echo hi", "backend": "local"}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any(k.get("status") == "cancelled" for _, k in updated), "terminal status не выставлен"
    assert cleaned, "backend cancel не вызван"
