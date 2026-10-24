"""Свипер осиротевших running-джобов: статус, границы, идемпотентность.

Прогон на SQLite-ветке db_adapter (PG-ветка — тот же SQL, но требует живого PG;
локально гейт исполняется без PG_DSN, поэтому проверяем ту ветку, что поедет в CI).
Живая data/roma.db не трогается: соединение подменено на tmp_path.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db
from billing import execution_worker as ew


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """db_adapter на временном SQLite + воркер, видящий этот же адаптер."""
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
    return _conn


def _insert(
    conn_factory,
    job_id: str,
    status: str,
    started_at: str | None,
    updated_at: str | None = None,
) -> None:
    with conn_factory() as c:
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, started_at, updated_at)"
            " VALUES (?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (job_id, "tenant-sweeper", status, started_at, updated_at),
        )
        c.commit()


def _ago(hours: float) -> str:
    """SQLite-формат datetime('now'): 'YYYY-MM-DD HH:MM:SS' в UTC."""
    ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _row(conn_factory, job_id: str) -> dict:
    with conn_factory() as c:
        row = c.execute(
            "SELECT status, error, completed_at, cost_usd FROM execution_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_old_running_becomes_timeout(sqlite_db):
    _insert(sqlite_db, "old-running", "running", _ago(3))

    swept = db.sweep_orphan_running_jobs(1200)

    assert swept == 1
    row = _row(sqlite_db, "old-running")
    assert row["status"] == "timeout"
    assert row["error"] == "orphaned: worker restart"
    assert row["completed_at"], "completed_at обязан быть проставлен"
    assert row["cost_usd"] == 0.0, "осиротевший джоб не должен получать cost"


def test_fresh_running_untouched(sqlite_db):
    _insert(sqlite_db, "fresh-running", "running", _ago(0.1))

    assert db.sweep_orphan_running_jobs(1200) == 0
    assert _row(sqlite_db, "fresh-running")["status"] == "running"


def test_queued_completed_and_null_started_untouched(sqlite_db):
    _insert(sqlite_db, "queued-old", "queued", _ago(5))
    _insert(sqlite_db, "completed-old", "completed", _ago(5))
    _insert(sqlite_db, "running-no-start", "running", None)

    assert db.sweep_orphan_running_jobs(1200) == 0
    assert _row(sqlite_db, "queued-old")["status"] == "queued"
    assert _row(sqlite_db, "completed-old")["status"] == "completed"
    assert _row(sqlite_db, "running-no-start")["status"] == "running"


def test_null_started_old_updated_at_becomes_timeout(sqlite_db):
    """started_at не проставлен, но строка висит с давних пор — это осиротевший джоб."""
    _insert(sqlite_db, "running-null-start-old-updated", "running", None, _ago(3))

    assert db.sweep_orphan_running_jobs(1200) == 1
    row = _row(sqlite_db, "running-null-start-old-updated")
    assert row["status"] == "timeout"
    assert row["error"] == "orphaned: worker restart"
    assert row["cost_usd"] == 0.0


def test_null_started_fresh_updated_at_untouched(sqlite_db):
    """started_at не проставлен, но работа идёт прямо сейчас — не трогаем."""
    _insert(sqlite_db, "running-null-start-fresh-updated", "running", None, _ago(0.1))

    assert db.sweep_orphan_running_jobs(1200) == 0
    assert _row(sqlite_db, "running-null-start-fresh-updated")["status"] == "running"


def test_sweep_is_idempotent(sqlite_db):
    _insert(sqlite_db, "orphan-a", "running", _ago(2))
    _insert(sqlite_db, "orphan-b", "running", _ago(4))

    assert db.sweep_orphan_running_jobs(1200) == 2
    assert db.sweep_orphan_running_jobs(1200) == 0
    assert _row(sqlite_db, "orphan-a")["status"] == "timeout"
    assert _row(sqlite_db, "orphan-b")["status"] == "timeout"


def test_timeout_must_be_positive(sqlite_db):
    with pytest.raises(ValueError):
        db.sweep_orphan_running_jobs(0)


def test_worker_default_timeout_is_twice_max_job_timeout():
    assert ew.MAX_JOB_TIMEOUT_SECONDS == 600
    assert ew.JOB_ORPHAN_TIMEOUT_SECONDS == ew.MAX_JOB_TIMEOUT_SECONDS * 2


def test_worker_sweep_uses_adapter_and_logs(sqlite_db, monkeypatch):
    _insert(sqlite_db, "orphan-worker", "running", _ago(3))
    logged: list[str] = []
    monkeypatch.setattr(
        ew.logger, "warning", lambda msg, *args, **kw: logged.append(msg % args)
    )

    swept = ew.sweep_orphans_once()

    assert swept == 1
    assert any("orphans_swept" in line for line in logged)
    assert _row(sqlite_db, "orphan-worker")["status"] == "timeout"


def test_worker_sweep_is_noop_before_init(sqlite_db, monkeypatch):
    monkeypatch.setattr(ew, "_db_adapter", None)
    assert ew.sweep_orphans_once() == 0
