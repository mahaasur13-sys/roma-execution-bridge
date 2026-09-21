"""F2: migration schema contract tests.

Covers the schema contract not already tested in ``test_migration_runner.py``
(which already asserts unique numeric prefixes + no-op without PG):

- no ``DROP DATABASE`` / ``DROP TABLE`` without ``IF EXISTS``
- re-applying via the runner is idempotent (second run = all skipped)
- after apply, the tables the code actually reads exist
- SQLite ``_ensure_*`` helpers are idempotent (two calls don't crash)

PG-dependent checks skip cleanly with an explicit reason when PG is not
reachable. SQLite checks run regardless.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_migrations", str(REPO_ROOT / "scripts" / "run_migrations.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pg_dsn() -> str | None:
    return os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")


def _pg_reachable() -> bool:
    dsn = _pg_dsn()
    if not dsn:
        return False
    try:
        import psycopg2

        conn = psycopg2.connect(dsn)
        conn.close()
        return True
    except Exception:
        return False


# Tables the code actually reads (grep: db_adapter, billing/pg_ledger,
# billing/pg_metering, auth/invites, auth/verification) and which the
# migrations create. Not invented from docs.
EXPECTED_TABLES = [
    "ledger_entries",
    "execution_jobs",
    "usage_events",
    "submit_idempotency_keys",
    "invite_codes",
    "invite_usage",
    "verification_tokens",
]


def test_no_drop_without_if_exists():
    """No migration may DROP DATABASE / DROP TABLE without IF EXISTS."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if re.match(r"(?i)^DROP\s+(DATABASE|TABLE)\b", stripped):
                assert (
                    "IF EXISTS" in stripped.upper()
                ), f"{path.name}: DROP without IF EXISTS: {stripped}"


def test_runner_reapply_is_idempotent(capsys):
    """Second runner pass applies nothing (all files already tracked)."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — migration re-apply contract requires live PG; issue: P1-C · expiry: 2026-12-31"
        )
    runner = _load_runner()
    assert runner.main() == 0
    capsys.readouterr()  # discard first pass output
    assert runner.main() == 0
    out = capsys.readouterr().out
    files = runner._migration_files()
    assert f"0 applied, {len(files)} skipped" in out


def test_migrations_create_expected_tables():
    """After apply, the tables the code reads must exist in PG."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — object-existence contract requires live PG; issue: P1-C · expiry: 2026-12-31"
        )
    runner = _load_runner()
    runner.main()

    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        cur = conn.cursor()
        for table in EXPECTED_TABLES:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            assert cur.fetchone()[0] is not None, f"table {table} not created"
    finally:
        conn.close()


def test_sqlite_ensure_helpers_are_idempotent():
    """SQLite _ensure_* helpers must be safe to call twice."""
    import db_adapter

    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    db_adapter._ensure_submit_idempotency_table(c)
    db_adapter._ensure_submit_idempotency_table(c)
    db_adapter._ensure_execution_jobs_table(c)
    db_adapter._ensure_execution_jobs_table(c)
    conn.close()
