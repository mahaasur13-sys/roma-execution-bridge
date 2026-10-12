"""RaaS-01 / migration 017: read-only preflight for the 016 unique index.

017 counts duplicate ``(tenant_id, job_id, event_type)`` groups for REAL job_ids
under the SAME predicate as 016 (`job_id IS NOT NULL AND job_id NOT IN
('manual','auto','')`) and raises one actionable error with the group count when
any exist. It never DELETEs/UPDATEs rows and never creates/drops the index.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_016 = REPO_ROOT / "migrations" / "016_usage_events_idempotency.sql"
MIGRATION_017 = REPO_ROOT / "migrations" / "017_usage_events_dedup_preflight.sql"

INDEX_NAME = "uq_usage_events_tenant_job_type"
FILENAME_017 = "017_usage_events_dedup_preflight.sql"


def _pg_dsn() -> str | None:
    return os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")


def _pg_reachable() -> bool:
    dsn = _pg_dsn()
    if not dsn:
        return False
    try:
        import psycopg2

        with psycopg2.connect(dsn) as conn:
            conn.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


def _run_migrations() -> None:
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "run_migrations.py")],
        check=True,
        capture_output=True,
    )


def _apply_body(conn, path: Path) -> None:
    """Apply one migration body in its own transaction, like run_migrations.py."""
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(path.read_text())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def _index_present(conn) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE tablename='usage_events' AND indexname=%s",
        (INDEX_NAME,),
    )
    return cur.fetchone() is not None


@pytest.mark.pg
def test_017_applied_without_duplicates():
    """Disposable without duplicates: 017 applies and lands in schema_migrations."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — 017 preflight requires live PG; "
            "issue: RaaS-01 · expiry: 2026-12-31"
        )
    _run_migrations()

    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM schema_migrations WHERE filename = %s", (FILENAME_017,)
        )
        assert cur.fetchone() is not None, "017 не отмечена в schema_migrations"
    finally:
        conn.close()


@pytest.mark.pg
def test_017_fails_on_duplicate_group_and_index_intact():
    """Two identical real-job rows → 017 raises with the count; index untouched."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — 017 preflight requires live PG; "
            "issue: RaaS-01 · expiry: 2026-12-31"
        )
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    tenant = f"raas-pre-{uuid.uuid4().hex[:8]}"
    job = f"job-{uuid.uuid4().hex[:8]}"
    try:
        _run_migrations()  # usage_events (+billed) + 016 index present, no dups
        conn.autocommit = True
        cur = conn.cursor()

        # Simulate the pre-016 live state: the unique index is not applied yet.
        cur.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")

        # Two identical rows for the same REAL job_id.
        for _ in range(2):
            cur.execute(
                "INSERT INTO usage_events (tenant_id, event_type, value, cost_usd, job_id, billed)"
                " VALUES (%s, 'gpu_usage', 10.0, 0.0001, %s, FALSE)",
                (tenant, job),
            )

        with pytest.raises(Exception) as exc:
            _apply_body(conn, MIGRATION_017)
        assert "duplicate groups = 1" in str(exc.value)

        # Read-only guard: rows unchanged, index still absent (017 touched neither).
        cur.execute(
            "SELECT count(*) FROM usage_events WHERE tenant_id=%s AND job_id=%s",
            (tenant, job),
        )
        assert cur.fetchone()[0] == 2
        assert not _index_present(conn)
    finally:
        # Restore a clean migrated state for any later PG test.
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM usage_events WHERE tenant_id=%s AND job_id=%s", (tenant, job)
        )
        if not _index_present(conn):
            _apply_body(conn, MIGRATION_016)
        conn.close()
