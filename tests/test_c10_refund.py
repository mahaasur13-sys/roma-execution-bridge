"""C10 — REFUND/CREDIT путь: возврат дебета, включая g5d (отмена после дебета)."""

import asyncio
import hashlib
import itertools
import sqlite3
import sys
import types

sys.path.insert(0, "/home/felix/astrofin-sentinel-platform/.queue-work/roma-execution-bridge")

import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402
from billing import finalize as fin  # noqa: E402
from billing.pg_ledger import PGBillingLedger  # noqa: E402

TENANT = "tenant-c10"


def test_refund_is_idempotent(monkeypatch):
    ledger = PGBillingLedger()
    calls = []

    def fake_pg(op, q, params, fetch=False):
        if op == "ledger_credit":
            lid = params[0]
            calls.append(("credit", lid))
            return [lid] if fetch else None
        if op == "ledger_credit_idem_lookup":
            return [(params[0],)] if fetch else None
        return None

    monkeypatch.setattr(ledger, "_pg_execute", fake_pg)
    r1 = ledger.refund(TENANT, 0.001, job_id="j1", reason="cancelled_after_debit")
    r2 = ledger.refund(TENANT, 0.001, job_id="j1", reason="cancelled_after_debit")
    assert r1 == r2  # идемпотентно по refund:<job_id>
    assert [c[0] for c in calls].count("credit") == 2  # два вызова, но один ledger_id


def test_worker_refunds_cancelled_job(tmp_path, monkeypatch):
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    class FakeLedger:
        def __init__(self):
            self.debits = {}
            self.refunds = []

        def get_tenant_balance(self, tid):
            return 100.0

        def debit_if_funds(self, tid, amount, currency="USD", idempotency_key=None, **meta):
            lid = "led-" + hashlib.sha256(f"{tid}:{idempotency_key}".encode()).hexdigest()[:24]
            if lid in self.debits:
                return lid, False
            self.debits[lid] = amount
            return lid, True

        def refund(self, tid, amount, currency="USD", job_id=None, reason="refund"):
            self.refunds.append((job_id, amount, reason))
            return "led-refund"

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    ledger = FakeLedger()
    monkeypatch.setattr(fin, "billing_ledger", ledger)
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    ticks = itertools.chain([1000.0], itertools.repeat(1100.0))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    async def fake_dispatch(**k):
        return {"backend": "local", "status": "running"}

    async def fake_status(job_id, backend_job_id):
        return {"status": "completed"}

    async def fake_cancel(job_id, tenant_id):
        return None

    monkeypatch.setattr(
        ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": fake_cancel}
    )

    import main

    real_finalize = fin.finalize_job_billing

    def finalize_then_cancel(*a, **k):
        res = real_finalize(*a, **k)
        db.update_execution_job("job-c10", status="cancelled", tenant_id=TENANT, if_status_not_in=db.CANCEL_BLOCKED_STATUSES)
        return res

    monkeypatch.setattr(main, "finalize_job_billing", finalize_then_cancel)

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'local', datetime('now', '-60 seconds'))",
            ("job-c10", TENANT),
        )
        c.commit()

    asyncio.run(ew._execute_and_bill_impl("job-c10", TENANT, {"backend": "local", "task": "x"}))

    # C10: деньги списаны, задача отменена → возврат.
    assert ledger.refunds, "отменённая задача должна вернуть дебет"
    assert ledger.refunds[0][0] == "job-c10"
    assert ledger.refunds[0][2] == "cancelled_after_debit"


def test_worker_refund_failure_marks_pending(tmp_path, monkeypatch):
    """C10 fail-closed: refund падает → durable-маркер refund_pending, не молча."""
    import billing.execution_worker as ew_mod

    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    class FailingRefundLedger:
        def get_tenant_balance(self, tid):
            return 100.0

        def debit_if_funds(self, tid, amount, currency="USD", idempotency_key=None, **meta):
            lid = "led-" + hashlib.sha256(f"{tid}:{idempotency_key}".encode()).hexdigest()[:24]
            return lid, True

        def refund(self, tid, amount, currency="USD", job_id=None, reason="refund"):
            raise RuntimeError("refund PG down (test)")

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    monkeypatch.setattr(fin, "billing_ledger", FailingRefundLedger())
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    ticks = itertools.chain([1000.0], itertools.repeat(1100.0))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(ew_mod, "asyncio", types.SimpleNamespace(sleep=_nosleep))

    async def fake_dispatch(**k):
        return {"backend": "local", "status": "running"}

    async def fake_status(job_id, backend_job_id):
        return {"status": "completed"}

    async def fake_cancel(job_id, tenant_id):
        return None

    monkeypatch.setattr(
        ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": fake_cancel}
    )

    import main

    real_finalize = fin.finalize_job_billing

    def finalize_then_cancel(*a, **k):
        res = real_finalize(*a, **k)
        db.update_execution_job("job-c10-fail", status="cancelled", tenant_id=TENANT, if_status_not_in=db.CANCEL_BLOCKED_STATUSES)
        return res

    monkeypatch.setattr(main, "finalize_job_billing", finalize_then_cancel)

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'local', datetime('now', '-60 seconds'))",
            ("job-c10-fail", TENANT),
        )
        c.commit()

    asyncio.run(ew._execute_and_bill_impl("job-c10-fail", TENANT, {"backend": "local", "task": "x"}))

    with _conn() as c:
        row = dict(c.execute("SELECT * FROM execution_jobs WHERE id=?", ("job-c10-fail",)).fetchone())
    # fail-closed: DEBIT не оставлен молча — на строке durable-маркер.
    assert row["error"] == "refund_pending:debit_exists", row


def test_refund_sweeper_retries_pending(tmp_path, monkeypatch):
    """C10 sweeper: cancelled-джоб с refund_pending → refund + снять маркер."""
    import main as main_mod

    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    refunded = []
    monkeypatch.setattr(main_mod, "refund_job", lambda tid, amount, job_id=None, reason="refund": refunded.append((job_id, amount)))

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, completed_at)"
            " VALUES (?, ?, 'cancelled', 'refund_pending:debit_exists', 0.001, 'local', datetime('now'))",
            ("job-sweep", TENANT),
        )
        c.commit()

    out = ew.sweep_refund_pending_once()
    assert out["ok"] == 1, out
    assert refunded == [("job-sweep", 0.001)], refunded

    with _conn() as c:
        row = dict(c.execute("SELECT error FROM execution_jobs WHERE id='job-sweep'").fetchone())
    assert row["error"] == "", "маркер refund_pending должен быть снят после успешного refund"
