#!/usr/bin/env python3
"""Apply migrations/*.sql to PostgreSQL in filename order, idempotently.

Tracks applied files in a `schema_migrations` table so re-running is a no-op
for already-applied files. Each file runs inside its own transaction (explicit
`BEGIN;` / `COMMIT;` wrappers are stripped so the runner owns the transaction).

PG-only: without `PG_DSN` / `DATABASE_URL` this exits cleanly as a no-op
(SQLite schema is handled idempotently by `db_adapter._ensure_*` helpers).

Usage:
    python scripts/run_migrations.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env_loader import load_env

load_env()


def _dsn() -> str | None:
    return os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")


def _migration_files() -> list[Path]:
    root = Path(__file__).resolve().parent.parent
    return sorted((root / "migrations").glob("*.sql"))


def _strip_txn_wrappers(sql: str) -> str:
    """Remove standalone BEGIN;/COMMIT; lines so the runner owns the transaction."""
    return "\n".join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in ("BEGIN;", "COMMIT;")
    )


def main() -> int:
    dsn = _dsn()
    if not dsn:
        print(
            "PG only: PG_DSN / DATABASE_URL not set — no-op (SQLite uses db_adapter)",
            file=sys.stderr,
        )
        return 0

    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename TEXT PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )

        files = _migration_files()
        applied = skipped = 0
        for path in files:
            name = path.name
            cur.execute("SELECT 1 FROM schema_migrations WHERE filename = %s", (name,))
            if cur.fetchone():
                print(f"skip    {name}")
                skipped += 1
                continue

            body = _strip_txn_wrappers(path.read_text())
            conn.autocommit = False
            try:
                cur.execute(body)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (name,)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
            print(f"applied {name}")
            applied += 1

        print(f"done: {applied} applied, {skipped} skipped")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
