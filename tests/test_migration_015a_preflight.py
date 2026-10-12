"""RaaS-01 / migration 015a: preflight that runs BEFORE the 016 unique index.

015a sorts before 016, so the runner checks for duplicate
``(tenant_id, job_id, event_type)`` groups (same predicate as 016) BEFORE 016
attempts to create ``uq_usage_events_tenant_job_type``. It is strictly read-only:
no DELETE/UPDATE and no create/drop of the index.

The destructive scenario (DROP INDEX to reproduce a duplicate-bearing table) is
confined to a throwaway PostgreSQL container that this test starts itself — it
never drops the index on the ambient/shared PG_DSN database.
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

MIGRATION_015A = REPO_ROOT / "migrations" / "015a_usage_events_dedup_preflight.sql"
INDEX_NAME = "uq_usage_events_tenant_job_type"
FILENAME_015A = "015a_usage_events_dedup_preflight.sql"


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
    body = "\n".join(
        line
        for line in path.read_text().splitlines()
        if line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(body)
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
def test_preflight_applied_without_duplicates():
    """015a applied (before 016) and the 016 index still gets created."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — 015a preflight requires live PG; "
            "issue: RaaS-01 · expiry: 2026-12-31"
        )
    _run_migrations()

    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM schema_migrations WHERE filename = %s", (FILENAME_015A,)
        )
        assert cur.fetchone() is not None, "015a не отмечена в schema_migrations"
        assert _index_present(conn), "016 index должен существовать после 015a"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Destructive scenario — confined to the test's own throwaway container.
# ---------------------------------------------------------------------------


def _start_own_container() -> tuple[str | None, str | None]:
    """Start a throwaway postgres:15 container; return (dsn, name) or (None, None)."""
    name = f"roma-preflight-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", name,
                "-e", "POSTGRES_USER=roma",
                "-e", "POSTGRES_PASSWORD=ci-ephemeral-localhost",
                "-e", "POSTGRES_DB=roma_test",
                "-p", "127.0.0.1::5432", "postgres:15-alpine",
            ],
            check=True,
            capture_output=True,
        )
    except Exception:
        return None, None
    try:
        port = subprocess.check_output(
            ["docker", "port", name, "5432/tcp"], text=True
        ).strip().split(":")[-1]
    except Exception:
        _stop_container(name)
        return None, None
    dsn = f"postgresql://roma:ci-ephemeral-localhost@127.0.0.1:{port}/roma_test"

    import time

    import psycopg2

    for _ in range(30):
        try:
            with psycopg2.connect(dsn, connect_timeout=2) as conn:
                conn.cursor().execute("SELECT 1")
            return dsn, name
        except Exception:
            time.sleep(1)
    _stop_container(name)
    return None, None


def _stop_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


def _same_db(a: str, b: str) -> bool:
    import urllib.parse

    def norm(dsn: str) -> tuple:
        p = urllib.parse.urlparse(dsn)
        host = (p.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", "[::1]"):
            host = "localhost"
        return (host, p.port or 5432, (p.path or "").lstrip("/"))

    return norm(a) == norm(b)


@pytest.mark.pg
def test_preflight_fails_on_duplicate_group_and_index_intact():
    """Two identical real-job rows → 015a raises; read-only (never on shared PG)."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — 015a preflight requires live PG; "
            "issue: RaaS-01 · expiry: 2026-12-31"
        )
    dsn, name = _start_own_container()
    if dsn is None:
        pytest.skip(
            "docker unavailable — destructive preflight test needs its own "
            "throwaway PG; issue: RaaS-01 · expiry: 2026-12-31"
        )
    try:
        # Safety guard: the destructive DROP INDEX may only run when the ambient
        # PG_DSN is THIS throwaway container, never a shared/production DB.
        ambient = _pg_dsn()
        if ambient and not _same_db(ambient, dsn):
            pytest.skip(
                "PG_DSN points not to this test's own throwaway container — "
                "destructive scenario is container-scoped; "
                "issue: RaaS-01 · expiry: 2026-12-31"
            )

        # Full migrations on the OWN container (index exists, 015a recorded).
        env = dict(os.environ, PG_DSN=dsn)
        env.pop("DATABASE_URL", None)
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "run_migrations.py")],
            check=True,
            capture_output=True,
            env=env,
        )

        import psycopg2

        conn = psycopg2.connect(dsn)
        tenant = f"raas-pre-{uuid.uuid4().hex[:8]}"
        job = f"job-{uuid.uuid4().hex[:8]}"
        try:
            conn.autocommit = True
            cur = conn.cursor()
            assert _index_present(conn), "precondition: index must exist after migrations"

            # Reproduce a duplicate-bearing table (index removed, own container only).
            cur.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
            for _ in range(2):
                cur.execute(
                    "INSERT INTO usage_events (tenant_id, event_type, value, cost_usd, job_id, billed)"
                    " VALUES (%s, 'gpu_usage', 10.0, 0.0001, %s, FALSE)",
                    (tenant, job),
                )

            with pytest.raises(Exception) as exc:
                _apply_body(conn, MIGRATION_015A)
            assert "duplicate groups = 1" in str(exc.value)

            # Read-only guard: rows unchanged, index still absent (015a touched neither).
            cur.execute(
                "SELECT count(*) FROM usage_events WHERE tenant_id=%s AND job_id=%s",
                (tenant, job),
            )
            assert cur.fetchone()[0] == 2
            assert not _index_present(conn)
        finally:
            conn.close()
    finally:
        _stop_container(name)
