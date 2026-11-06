"""OT-80 — get_billing_pending_jobs фильтрует paused no_funds до LIMIT."""

import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402

TENANT = "tenant-ot80"


def test_exclude_ids_filtered_before_limit(tmp_path, monkeypatch):
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        for i in range(1, 6):
            c.execute(
                "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, completed_at)"
                " VALUES (?, ?, 'billing_pending', '', 0.0, 'local', datetime('now', ?))",
                (f"j-{i}", TENANT, f"-{i} hours"),
            )
        c.commit()

    rows = db.get_billing_pending_jobs(limit=3, exclude_ids=["j-1", "j-2"])
    ids = [r["id"] for r in rows]
    assert len(ids) == 3, ids
    assert "j-1" not in ids and "j-2" not in ids, ids
    # старые первыми (completed_at ASC): j-5 (5h назад) → j-4 → j-3
    assert ids == ["j-5", "j-4", "j-3"], ids
