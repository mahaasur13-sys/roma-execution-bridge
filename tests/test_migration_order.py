"""RaaS-01: migration applied-order (015 → 015a → 016) + 015a read-only contract.

The 015a preflight must run BEFORE 016 creates the unique index. This is a
behavioural check that, after the runner applies migrations, the recorded order
in `schema_migrations` is 015 → 015a → 016 (not merely that the filenames sort
that way). The static check pins that 015a itself never issues DML/DDL.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_015A = REPO_ROOT / "migrations" / "015a_usage_events_dedup_preflight.sql"

FILES_015 = "015_usage_events_billed.sql"
FILES_015A = "015a_usage_events_dedup_preflight.sql"
FILES_016 = "016_usage_events_idempotency.sql"


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


def test_015a_is_readonly():
    """015a's executable SQL never issues DELETE/UPDATE/CREATE/DROP."""
    sql = MIGRATION_015A.read_text(encoding="utf-8")
    # Strip `--` line comments: the header prose legitimately mentions those
    # words, but the executable body must not contain them.
    executable = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    match = re.search(r"\b(?:DELETE|UPDATE|CREATE|DROP)\b", executable, re.IGNORECASE)
    assert match is None, f"015a содержит запрещённый оператор: {match.group(0)}"


@pytest.mark.pg
def test_migration_applied_order_015_015a_016():
    """After run_migrations, the applied order is 015 → 015a → 016."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — applied-order check requires live PG; "
            "issue: RaaS-01 · expiry: 2026-12-31"
        )
    _run_migrations()

    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        cur = conn.cursor()
        cur.execute("SELECT filename FROM schema_migrations ORDER BY applied_at")
        names = [row[0] for row in cur.fetchall()]

        for expected in (FILES_015, FILES_015A, FILES_016):
            assert expected in names, f"{expected} не применена: {names}"

        i_015 = names.index(FILES_015)
        i_015a = names.index(FILES_015A)
        i_016 = names.index(FILES_016)
        assert i_015 < i_015a < i_016, (
            f"applied-порядок нарушен: ожидается 015 → 015a → 016, получено {names}"
        )
    finally:
        conn.close()
