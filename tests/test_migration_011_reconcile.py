"""G-AUDIT-DDL-DRIFT (reconciling): прод-безопасность migrations/011_audit_events.sql.

Акт 2 аудита P3.10: на проде таблица `audit_events` могла уже существовать из
рантайм-бутстрапа со схемой БЕЗ `created_at` (факт из INSERT db_adapter.py) и с
историей эпохи double-write (дубли по `(tenant_id, event_type, entity_id)`).
Миграция обязана: не падать на CREATE (IF NOT EXISTS), сверить сигнатуру колонок,
дедуплицировать keep-ранняя (ctid) с логом removed, сохранить безключевые
(`entity_id='unknown'`) и кросс-тенантные строки, создать частичный UNIQUE,
повторный накат — no-op.
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

# prod-форма: колонки из INSERT db_adapter.py, БЕЗ created_at, data — JSONB.
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


@pytest.fixture()
def prod_form():
    """audit_events в прод-форме (рантайм-бутстрап, без created_at) + дубли эпохи double-write."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — reconciling audit requires live PG; issue: P1-C · expiry: 2026-12-31"
        )
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS audit_events")
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
        # восстановить чистую migrated-форму для последующих тестов (schema_migrations
        # уже содержит 011, поэтому раннер её повторно не накатит)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS audit_events")
        _apply_011(conn)
        conn.close()


@pytest.mark.pg
def test_011_reconciles_prod_form_with_duplicates(prod_form):
    """CREATE не падает; дубли сняты keep-ранняя; unknown/кросс-тенант живы; UNIQUE создан."""
    conn = prod_form
    _apply_011(conn)

    conn.autocommit = True
    cur = conn.cursor()

    # keep-ранняя: job-A → ровно одна строка, и это a1 (ранняя по ctid)
    cur.execute("SELECT id FROM audit_events WHERE entity_id='job-A'")
    ids = [r[0] for r in cur.fetchall()]
    assert ids == ["a1"], ids

    # два unknown одной задачи пережили частичный индекс (T2-семантика)
    cur.execute("SELECT count(*) FROM audit_events WHERE entity_id='unknown'")
    assert cur.fetchone()[0] == 2

    # одинаковый job_id у разных tenant'ов — оба живы
    cur.execute("SELECT count(*) FROM audit_events WHERE entity_id='job-X'")
    assert cur.fetchone()[0] == 2

    # частичный UNIQUE жив
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE tablename='audit_events' AND indexname='audit_events_dedupe_uidx'"
    )
    assert cur.fetchone() is not None


@pytest.mark.pg
def test_011_reapply_is_noop(prod_form):
    """Повторный накат — no-op: вторая дедупликация не снимает ни одной строки."""
    conn = prod_form
    _apply_011(conn)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    n_before = cur.fetchone()[0]

    _apply_011(conn)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    assert cur.fetchone()[0] == n_before
