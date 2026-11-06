"""OT-79 — свипер billing_pending должен останавливаться после pg_error.

При недоступном PG каждый job даёт ~3s connection timeout; перебор всех 50 задач
блокирует event loop. Фикс: break после pg_error + вызов свипера через
asyncio.to_thread (в poll_and_execute). Здесь тест только на break.
"""

import itertools
import sqlite3
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402
from billing.pg_ledger import PGUnavailableError  # noqa: E402

TENANT = "tenant-ot79"


def test_sweep_breaks_after_pg_error(tmp_path, monkeypatch):
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    ticks = itertools.repeat(10_000.0)
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        for jid in ("j-a", "j-b", "j-c"):
            c.execute(
                "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, completed_at)"
                " VALUES (?, ?, 'billing_pending', 'billing:pg_error', 0.0, 'local', datetime('now'))",
                (jid, TENANT),
            )
        c.commit()

    import main

    calls = []

    def fake_finalize(tenant_id, job_id, *a, **k):
        calls.append(job_id)
        raise PGUnavailableError("pg down (test)")

    monkeypatch.setattr(main, "finalize_job_billing", fake_finalize)

    out = ew.sweep_billing_pending_once(now=10_000.0)
    assert out["pg_error"] == 1
    assert len(calls) == 1, f"после pg_error цикл должен остановиться, но обработано {calls!r}"
