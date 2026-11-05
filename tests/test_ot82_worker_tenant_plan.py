"""OT-82 — воркер/свипер используют план арендатора из БД, а не хардкод "free"."""

import asyncio
import hashlib
import itertools
import os
import sqlite3
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402
from billing import finalize as fin  # noqa: E402

TENANT = "tenant-ot82"


class FakeLedger:
    def __init__(self, balance=100.0):
        self.balance = balance
        self.debits = {}

    def get_tenant_balance(self, tenant_id):
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        lid = "led-" + hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()[:24]
        if lid in self.debits:
            return lid, False
        if self.balance < amount:
            return None, False
        self.balance -= amount
        self.debits[lid] = amount
        return lid, True


def test_tenant_plan_from_db(monkeypatch):
    class FakeDb:
        def get_tenant(self, tid):
            return {"plan": "pro"}

    monkeypatch.setattr(ew, "_db_adapter", FakeDb())
    assert ew._tenant_plan("t") == "pro"


def test_tenant_plan_fallback_free(monkeypatch):
    class FakeDb:
        def get_tenant(self, tid):
            return None

    monkeypatch.setattr(ew, "_db_adapter", FakeDb())
    assert ew._tenant_plan("t") == "free"


def test_worker_passes_tenant_plan_to_finalize(tmp_path, monkeypatch):
    import main

    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    monkeypatch.setattr(db, "get_tenant", lambda tid: {"plan": "pro"})
    monkeypatch.setattr(fin, "billing_ledger", FakeLedger())
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    ticks = itertools.chain([1000.0], itertools.repeat(1100.0))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    async def fake_dispatch(**k):
        return {"backend": "local", "status": "running"}

    async def fake_status(job_id, backend_job_id):
        return {"status": "completed"}

    monkeypatch.setattr(ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": None})

    captured = {}

    def fake_finalize(tenant_id, job_id, gpu_sec, plan_name, backend=None):
        captured["plan"] = plan_name
        return "ok"

    monkeypatch.setattr(main, "finalize_job_billing", fake_finalize)

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'local', datetime('now', '-60 seconds'))",
            ("job-ot82", TENANT),
        )
        c.commit()

    asyncio.run(ew._execute_and_bill_impl("job-ot82", TENANT, {"backend": "local", "task": "echo hi"}))
    assert captured["plan"] == "pro", captured
