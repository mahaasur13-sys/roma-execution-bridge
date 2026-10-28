#!/usr/bin/env python3
"""Apply ``migrations/*.sql`` as ``roma_owner`` under an admin DSN.

Why this exists next to ``run_migrations.py``: that runner connects as the role
in the DSN and therefore creates objects under whatever role the deploy happens
to use. Here the admin DSN is only the *connection*: every migration body runs
inside ``SET LOCAL ROLE roma_owner``, so new tables land under the owning role
and the runtime role ``roma_app`` never needs DDL (``ROMA_DB_INIT_DDL=0``).

Guarantees:
  * DSN is never printed (stdout, stderr, argv, tracebacks).
  * Fail-closed when the DSN or the ``roma_owner`` role is missing.
  * Each migration is one transaction: ``SET LOCAL ROLE`` + body + ledger row.
  * After applying, objects in ``public`` not owned by ``roma_owner`` fail the run.

Usage:
    python scripts/apply_migrations.py --dry-run --dsn-file /root/.sealed/roma-pg-dsn.txt
    ROMA_ADMIN_PG_DSN=... python scripts/apply_migrations.py

Exit codes: 0 ok, 1 SQL/ownership failure, 2 refused (config/role).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_migrations  # noqa: E402 — shared helpers; also applies load_env()

OWNER_ROLE = "roma_owner"

OWNERSHIP_SQL = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p', 'S', 'v', 'm')
  AND pg_get_userbyid(c.relowner) <> %s
UNION
SELECT p.proname
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND pg_get_userbyid(p.proowner) <> %s
ORDER BY 1
"""

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _dsn_from_file(path: Path) -> str | None:
    """Last non-comment line: a bare DSN or ``KEY=DSN`` (the key is dropped)."""
    value = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, rest = line.partition("=")
        value = rest.strip() if sep and _KEY_RE.fullmatch(key) else line
    return value or None


def admin_dsn(dsn_file: str | None) -> str | None:
    """Env ``ROMA_ADMIN_PG_DSN`` wins; else the file given by ``--dsn-file``."""
    dsn = os.environ.get("ROMA_ADMIN_PG_DSN", "").strip()
    if dsn:
        return dsn
    if dsn_file:
        return _dsn_from_file(Path(dsn_file))
    return None


def _connect(dsn: str):
    import psycopg2

    return psycopg2.connect(dsn)


def role_present(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (OWNER_ROLE,))
        return cur.fetchone() is not None


def applied_names(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.schema_migrations')")
        if cur.fetchone()[0] is None:
            return set()
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def pending(files: list[Path], applied: set[str]) -> list[Path]:
    """Files not recorded in the ledger, in filename order."""
    return [path for path in files if path.name not in applied]


def ensure_ledger(conn) -> None:
    """Create the ledger if absent and pin it to ``roma_owner`` (idempotent).

    The ledger itself is created by the admin connection, so its owner is set
    explicitly — otherwise the ownership audit would flag the runner's own table.
    """
    with conn.cursor() as cur:
        cur.execute(run_migrations.LEDGER_DDL)
        cur.execute(f"ALTER TABLE schema_migrations OWNER TO {OWNER_ROLE}")


def apply_pending(conn, files: list[Path]) -> int:
    """Apply each file in its own transaction as ``roma_owner``."""
    for path in files:
        body = run_migrations._strip_txn_wrappers(path.read_text(encoding="utf-8"))
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL ROLE {OWNER_ROLE}")
                cur.execute(body)
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
        print(f"applied {path.name}")
    return len(files)


def foreign_owners(conn) -> list[str]:
    """Objects in ``public`` whose owner is not ``roma_owner``."""
    with conn.cursor() as cur:
        cur.execute(OWNERSHIP_SQL, (OWNER_ROLE, OWNER_ROLE))
        return [row[0] for row in cur.fetchall()]


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Apply migrations as {OWNER_ROLE}.")
    parser.add_argument("--dry-run", action="store_true", help="list pending migrations only")
    parser.add_argument("--dsn-file", help="file holding the admin DSN (used when ROMA_ADMIN_PG_DSN is unset)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    try:
        dsn = admin_dsn(args.dsn_file)
    except OSError as exc:
        print(f"cannot read --dsn-file: {exc.strerror}", file=sys.stderr)
        return 2

    if not dsn:
        print("ROMA_ADMIN_PG_DSN or --dsn-file is required (fail-closed)", file=sys.stderr)
        return 2

    conn = _connect(dsn)
    try:
        conn.autocommit = True
        if not role_present(conn):
            print(f"role {OWNER_ROLE} is missing — refusing (fail-closed)", file=sys.stderr)
            return 2

        files = run_migrations._migration_files()
        todo = pending(files, applied_names(conn))

        if args.dry_run:
            for path in todo:
                print(f"pending {path.name}")
            print(f"dry-run: {len(todo)} pending of {len(files)}")
            return 0

        ensure_ledger(conn)
        try:
            apply_pending(conn, todo)
        except Exception as exc:  # noqa: BLE001 — rolled back; nothing else is applied
            print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        bad = foreign_owners(conn)
        if bad:
            print(f"objects in public not owned by {OWNER_ROLE}: {', '.join(bad)}", file=sys.stderr)
            return 1
        print(f"done: {len(todo)} applied of {len(files)}; all objects owned by {OWNER_ROLE}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
