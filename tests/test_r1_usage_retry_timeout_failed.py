"""R1: usage-запись не теряется для timeout/failed — job уходит в billing_pending."""

import asyncio
import hashlib
import itertools
import sqlite3
import types


import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402
from billing import finalize as fin  # noqa: E402
from billing.pg_connection import PGUnavailableError  # noqa: E402

TENANT = "tenant-r1-retry"


def _run_worker_timeout(tmp_path, monkeypatch, exec_status):
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    class FakeLedger:
        def get_tenant_balance(self, tid):
            return 100.0

        def debit_if_funds(self, tid, amount, currency="USD", idempotency_key=None, **meta):
            lid = "led-" + hashlib.sha256(f"{tid}:{idempotency_key}".encode()).hexdigest()[:24]
            return lid, True

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    monkeypatch.setattr(fin, "billing_ledger", FakeLedger())
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    ticks = itertools.chain([1000.0], itertools.repeat(1100.0))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    async def fake_dispatch(**k):
        return {"backend": "local", "status": "running"}

    async def fake_status(job_id, backend_job_id):
        return {"status": exec_status}

    async def fake_cancel(job_id, tenant_id):
        return None

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": fake_cancel})
    monkeypatch.setattr(ew, "asyncio", types.SimpleNamespace(sleep=_nosleep))

    import main

    def finalize_raises(*a, **k):
        raise PGUnavailableError("usage write down (test)")

    monkeypatch.setattr(main, "finalize_job_billing", finalize_raises)

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'local', datetime('now', '-60 seconds'))",
            ("job-r1", TENANT),
        )
        c.commit()

    asyncio.run(ew._execute_and_bill_impl("job-r1", TENANT, {"backend": "local", "task": "x"}))
    with _conn() as c:
        return dict(c.execute("SELECT * FROM execution_jobs WHERE id='job-r1'").fetchone())


def test_timeout_billing_failure_goes_billing_pending(tmp_path, monkeypatch):
    row = _run_worker_timeout(tmp_path, monkeypatch, "timeout")
    assert row["status"] == "billing_pending", f"timeout + pg_error должен уйти в billing_pending: {row['status']!r}"


def test_failed_billing_failure_goes_billing_pending(tmp_path, monkeypatch):
    row = _run_worker_timeout(tmp_path, monkeypatch, "failed")
    assert row["status"] == "billing_pending", f"failed + pg_error должен уйти в billing_pending: {row['status']!r}"


def test_sweeper_restores_timeout_status_after_billing(tmp_path, monkeypatch):
    """R1: свипер после дотарификации не ставит completed — восстанавливает timeout."""
    import main as main_mod

    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    monkeypatch.setattr(main_mod, "finalize_job_billing", lambda *a, **k: "ok")

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, duration_seconds, cost_usd, backend, completed_at)"
            " VALUES (?, ?, 'billing_pending', 'billing:pg_error:exec=timeout', 100.0, 0.0, 'local', datetime('now'))",
            ("job-restore", TENANT),
        )
        c.commit()

    out = ew.sweep_billing_pending_once()
    assert out["ok"] == 1, out

    with _conn() as c:
        row = dict(c.execute("SELECT status, error FROM execution_jobs WHERE id='job-restore'").fetchone())
    assert row["status"] == "timeout", f"свипер не должен ставить completed: {row['status']!r}"
    assert row["error"] == "", row


def _seed_pending(tmp_path, monkeypatch, error):
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, duration_seconds, cost_usd, backend, completed_at)"
            " VALUES (?, ?, 'billing_pending', ?, 100.0, 0.0, 'local', datetime('now'))",
            ("job-br", TENANT, error),
        )
        c.commit()
    return _conn


def test_sweeper_mismatch_preserves_exec_status(tmp_path, monkeypatch):
    import main as main_mod

    _conn = _seed_pending(tmp_path, monkeypatch, "billing:pg_error:exec=timeout")
    monkeypatch.setattr(main_mod, "finalize_job_billing", lambda *a, **k: "mismatch")

    out = ew.sweep_billing_pending_once()
    assert out["mismatch"] == 1, out
    with _conn() as c:
        row = dict(c.execute("SELECT status FROM execution_jobs WHERE id='job-br'").fetchone())
    assert row["status"] == "timeout", f"mismatch не должен подменять exec-статус: {row['status']!r}"


def test_sweeper_skip_moves_out_of_billing_pending(tmp_path, monkeypatch):
    import main as main_mod

    _conn = _seed_pending(tmp_path, monkeypatch, "billing:pg_error:exec=failed")
    monkeypatch.setattr(main_mod, "finalize_job_billing", lambda *a, **k: "skip")

    out = ew.sweep_billing_pending_once()
    assert out["skip"] == 1, out
    with _conn() as c:
        row = dict(c.execute("SELECT status, error, cost_usd FROM execution_jobs WHERE id='job-br'").fetchone())
    assert row["status"] == "failed", f"skip должен вывести из billing_pending в exec-статус: {row['status']!r}"
    assert row["error"] == "", row
    assert float(row["cost_usd"] or 0) == 0.0, (
        f"skip не должен писать пересчитанный cost_usd без DEBIT: {row['cost_usd']!r}"
    )


def test_skip_does_not_overwrite_cancelled(tmp_path, monkeypatch):
    """R1: skip-ветка с CAS не затирает cancelled, выставленный гонкой /cancel."""
    import main as main_mod

    _conn = _seed_pending(tmp_path, monkeypatch, "billing:pg_error:exec=failed")

    def finalize_races_to_cancelled(*a, **k):
        # гонка: между чтением свипером и финальным UPDATE /cancel ставит cancelled.
        with _conn() as c:
            c.execute("UPDATE execution_jobs SET status='cancelled' WHERE id='job-br'")
            c.commit()
        return "skip"

    monkeypatch.setattr(main_mod, "finalize_job_billing", finalize_races_to_cancelled)

    ew.sweep_billing_pending_once()
    with _conn() as c:
        row = dict(c.execute("SELECT status FROM execution_jobs WHERE id='job-br'").fetchone())
    assert row["status"] == "cancelled", f"skip не должен затереть cancelled: {row['status']!r}"


def test_mismatch_does_not_overwrite_cancelled(tmp_path, monkeypatch):
    """R1: mismatch-ветка с CAS не затирает cancelled, выставленный гонкой /cancel."""
    import main as main_mod

    _conn = _seed_pending(tmp_path, monkeypatch, "billing:pg_error:exec=timeout")

    def finalize_races_to_cancelled(*a, **k):
        # гонка: между чтением свипером и финальным UPDATE /cancel ставит cancelled.
        with _conn() as c:
            c.execute("UPDATE execution_jobs SET status='cancelled' WHERE id='job-br'")
            c.commit()
        return "mismatch"

    monkeypatch.setattr(main_mod, "finalize_job_billing", finalize_races_to_cancelled)

    ew.sweep_billing_pending_once()
    with _conn() as c:
        row = dict(c.execute("SELECT status FROM execution_jobs WHERE id='job-br'").fetchone())
    assert row["status"] == "cancelled", f"mismatch не должен затереть cancelled: {row['status']!r}"
