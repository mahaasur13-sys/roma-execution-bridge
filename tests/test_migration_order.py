"""RaaS-01: migration applied-order (this run: 015 → 015a → 016) + 015a read-only contract.

The 015a preflight must run BEFORE 016 creates the unique index. The order check
proves the order of THIS run on a clean disposable database (not stale ledger
records, not timestamp-derived order): the runner is executed against a throwaway
container and the order is taken from its `applied {name}` stdout lines in the
order they were printed. `schema_migrations` is asserted only as a fact that the
files were recorded.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_015A = REPO_ROOT / "migrations" / "015a_usage_events_dedup_preflight.sql"

FILES_015 = "015_usage_events_billed.sql"
FILES_015A = "015a_usage_events_dedup_preflight.sql"
FILES_016 = "016_usage_events_idempotency.sql"
ORDERED = (FILES_015, FILES_015A, FILES_016)


def _pg_dsn() -> str | None:
    return os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")


def _assert_applied_order(names: list[str]) -> None:
    """Raise AssertionError unless 015 → 015a → 016 is the recorded order."""
    positions: list[int] = []
    for expected in ORDERED:
        assert expected in names, f"{expected} не применена: {names}"
        positions.append(names.index(expected))
    assert positions == sorted(positions), (
        f"applied-порядок нарушен: ожидается 015 → 015a → 016, получено {names}"
    )


def _applied_names_from_stdout(stdout: str) -> list[str]:
    """Names of migrations applied in THIS run, in the order the runner printed them."""
    return [
        line.split(" ", 1)[1].strip()
        for line in stdout.splitlines()
        if line.startswith("applied ")
    ]


def _require_ambient_pg(dsn: str) -> None:
    """DSN is set: an unreachable PG must FAIL the test, not skip it."""
    import psycopg2

    try:
        with psycopg2.connect(dsn, connect_timeout=5) as conn:
            conn.cursor().execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — fail-closed: down == fail, not skip
        pytest.fail(f"PG_DSN задан, но PG не отвечает (fail, не skip): {exc}")


def _start_own_container() -> tuple[str | None, str | None]:
    """Start a throwaway postgres:15 container; return (dsn, name) or (None, None)."""
    name = f"roma-order-{uuid.uuid4().hex[:8]}"
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


def test_order_helper_rejects_reverse_order():
    """Negative: a synthetic reverse order must fail the order assertion."""
    reverse = [FILES_016, FILES_015A, FILES_015]
    with pytest.raises(AssertionError):
        _assert_applied_order(reverse)


@pytest.mark.pg
def test_migration_applied_order_015_015a_016():
    """On a clean disposable schema, THIS run applies 015 → 015a → 016."""
    dsn = _pg_dsn()
    if not dsn:
        pytest.skip(
            "PG_DSN not set — applied-order check requires PG; "
            "issue: RaaS-01 · expiry: 2026-12-31"
        )
    _require_ambient_pg(dsn)

    own_dsn, name = _start_own_container()
    if own_dsn is None:
        pytest.fail("не удалось поднять чистую disposable-схему (docker) — fail, не skip")

    try:
        env = dict(os.environ, PG_DSN=own_dsn)
        env.pop("DATABASE_URL", None)
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "run_migrations.py")],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        # Order is taken from the runner's `applied {name}` stdout lines — the
        # order of THIS run's execution, not a timestamp-derived order.
        applied = _applied_names_from_stdout(result.stdout)
        _assert_applied_order(applied)

        # schema_migrations is only a fact of application (recorded), not an
        # ordering source.
        import psycopg2

        conn = psycopg2.connect(own_dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT filename FROM schema_migrations")
            recorded = {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

        for expected in ORDERED:
            assert expected in recorded, (
                f"{expected} не записана в schema_migrations"
            )
    finally:
        _stop_container(name)
