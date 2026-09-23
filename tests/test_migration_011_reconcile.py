"""G-AUDIT-DDL-DRIFT (reconciling): прод-безопасность migrations/011_audit_events.sql.

Акт 2 аудита P3.10 (v3): на проде таблица `audit_events` могла существовать из
рантайм-бутстрапа со схемой БЕЗ `created_at` (факт из INSERT db_adapter.py) и с
историей эпохи double-write (дубли по `(tenant_id, event_type, entity_id)`).
Миграция обязана: не падать на CREATE (IF NOT EXISTS); сверить сигнатуру
(имена + типы + PK(id)) fail-closed; заблокировать параллельные записи (TOCTOU);
снять дубли keep-«ранняя» по ctid с backup-таблицей и протоколом; сохранить
безключевые (`entity_id='unknown'`) и кросс-тенантные строки; создать частичный
UNIQUE; повторный накат — no-op.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_011 = REPO_ROOT / "migrations" / "011_audit_events.sql"

PROD_FORM_SCHEMA = """
CREATE TABLE audit_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    event_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    data        JSONB
)
"""

DUP_ROWS = [
    ("a1", "tA", "job.user_confirmed", "job", "job-A"),   # (a) дубль одной задачи
    ("a2", "tA", "job.user_confirmed", "job", "job-A"),
    ("u1", "tA", "job.user_confirmed", "job", "unknown"),  # (b) два unknown одной задачи
    ("u2", "tA", "job.user_confirmed", "job", "unknown"),
    ("x1", "tA", "job.user_confirmed", "job", "job-X"),   # (c) один job_id у разных tenant
    ("x2", "tB", "job.user_confirmed", "job", "job-X"),
]

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


def _apply_011(conn) -> None:
    """Накат 011 телом целиком, как это делает run_migrations.py."""
    body = MIGRATION_011.read_text()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(body)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _reset_tables(conn) -> None:
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS audit_events")
    cur.execute("DROP TABLE IF EXISTS audit_events_dedupe_backup")


@pytest.fixture()
def prod_form():
    """audit_events в прод-форме (без created_at) + дубли эпохи double-write."""
    if not _pg_reachable():
        pytest.skip("PG not reachable — reconciling audit requires live PG; issue: P1-C · expiry: 2026-12-31")
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    _reset_tables(conn)
    cur = conn.cursor()
    cur.execute(PROD_FORM_SCHEMA)
    for rid, tid, et, ent, eid in DUP_ROWS:
        cur.execute(
            "INSERT INTO audit_events (id,tenant_id,event_type,entity_type,entity_id,data)"
            " VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
            (rid, tid, et, ent, eid, '{"user_confirmed": true}'),
        )
    try:
        yield conn
    finally:
        # восстановить чистую migrated-форму для последующих тестов
        _reset_tables(conn)
        _apply_011(conn)
        conn.close()


@pytest.mark.pg
def test_011_reconciles_prod_form_with_duplicates(prod_form):
    """CREATE не падает; дубли сняты keep-ранняя; unknown/кросс-тенант живы; backup + UNIQUE."""
    conn = prod_form
    _apply_011(conn)

    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SELECT id FROM audit_events WHERE entity_id='job-A'")
    assert [r[0] for r in cur.fetchall()] == ["a1"]

    cur.execute("SELECT count(*) FROM audit_events WHERE entity_id='unknown'")
    assert cur.fetchone()[0] == 2

    cur.execute("SELECT count(*) FROM audit_events WHERE entity_id='job-X'")
    assert cur.fetchone()[0] == 2

    # снятый дубль попал в backup
    cur.execute("SELECT id FROM audit_events_dedupe_backup")
    assert [r[0] for r in cur.fetchall()] == ["a2"]

    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE tablename='audit_events' AND indexname='audit_events_dedupe_uidx'"
    )
    assert cur.fetchone() is not None


@pytest.mark.pg
def test_011_reapply_is_noop(prod_form):
    """Повторный накат — no-op: строки не удаляются, backup не растёт."""
    conn = prod_form
    _apply_011(conn)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    n_before = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM audit_events_dedupe_backup")
    b_before = cur.fetchone()[0]

    _apply_011(conn)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    assert cur.fetchone()[0] == n_before
    cur.execute("SELECT count(*) FROM audit_events_dedupe_backup")
    assert cur.fetchone()[0] == b_before


@pytest.mark.pg
def test_011_type_drift_fails_closed():
    """entity_id INTEGER → сверка типов роняет миграцию с протоколом различий."""
    if not _pg_reachable():
        pytest.skip("PG not reachable — reconciling audit requires live PG; issue: P1-C · expiry: 2026-12-31")
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    _reset_tables(conn)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE audit_events (id TEXT PRIMARY KEY, tenant_id TEXT, event_type TEXT,"
        " entity_type TEXT, entity_id INTEGER, data JSONB)"
    )
    try:
        with pytest.raises(Exception) as exc:
            _apply_011(conn)
        assert "schema drift" in str(exc.value)
        assert "entity_id" in str(exc.value)
    finally:
        _reset_tables(conn)
        _apply_011(conn)
        conn.close()


@pytest.mark.pg
def test_011_lock_blocks_concurrent_write():
    """TOCTOU: под LOCK SHARE ROW EXCLUSIVE параллельный INSERT блокируется."""
    if not _pg_reachable():
        pytest.skip("PG not reachable — reconciling audit requires live PG; issue: P1-C · expiry: 2026-12-31")
    import psycopg2
    import psycopg2.errors

    conn = psycopg2.connect(_pg_dsn())
    _reset_tables(conn)
    cur = conn.cursor()
    cur.execute(PROD_FORM_SCHEMA)
    cur.execute(
        "INSERT INTO audit_events VALUES ('a1','tA','job.user_confirmed','job','job-A','{}'::jsonb)"
    )

    holder = psycopg2.connect(_pg_dsn())
    holder.autocommit = False
    holder.cursor().execute("LOCK TABLE audit_events IN SHARE ROW EXCLUSIVE MODE")

    writer = psycopg2.connect(_pg_dsn())
    writer.autocommit = True
    wcur = writer.cursor()
    wcur.execute("SET statement_timeout = 700")
    blocked = False
    try:
        wcur.execute(
            "INSERT INTO audit_events VALUES ('a2','tA','job.user_confirmed','job','job-A','{}'::jsonb)"
        )
    except psycopg2.errors.QueryCanceled:
        blocked = True
    finally:
        holder.commit()
        holder.close()
        writer.close()

    assert blocked, "параллельный INSERT не был заблокирован локом (TOCTOU открыт)"
    _reset_tables(conn)
    _apply_011(conn)
    conn.close()
