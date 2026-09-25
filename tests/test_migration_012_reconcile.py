"""G-AUDIT-MIGRATION-HARDENING (PR #95 :50/:69): migrations/012_audit_events_reconcile_order.sql.

Согласующий повторный дедуп с достоверным порядком (created_at, id) вместо ctid.
011 НЕ редактируется (применён). Идемпотентен: повторный накат = 0 изменений.
Авто-колонки (identity/generated) не вставляются явно.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_012 = REPO_ROOT / "migrations" / "012_audit_events_reconcile_order.sql"

# Прод-форма: 6 базовых колонок + created_at (рантайм-бутстрап).
PROD_FORM_SCHEMA = """
CREATE TABLE audit_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    event_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    data        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


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


def _apply_012(conn) -> None:
    body = MIGRATION_012.read_text()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(body)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _reset_tables(conn) -> None:
    """Снести таблицы предмета И книгу schema_migrations.

    Почему книгу тоже: прогон идёт в одной БД (roma_test). Если снести таблицы,
    но оставить отметки, следующий run_migrations.py сочтёт миграции
    применёнными («0 applied, 13 skipped»), и контракт существования объектов
    (tests/test_migration_schema.py) покраснеет на пустой БД.
    """
    dsn = _pg_dsn() or ""
    if "roma_test" not in dsn and os.environ.get("ROMA_TEST_DSN") != "1":
        raise RuntimeError(
            "ОТКАЗ: DROP TABLE разрешён только на тестовой БД (DSN обязан содержать "
            "'roma_test' либо выставить ROMA_TEST_DSN=1). Прод-инстанс не трогаем."
        )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS audit_events")
    cur.execute("DROP TABLE IF EXISTS audit_events_dedupe_backup")
    cur.execute("DROP TABLE IF EXISTS audit_events_dedupe_backup_full")
    cur.execute("DROP TABLE IF EXISTS schema_migrations")


def _cleanup(conn) -> None:
    try:
        _reset_tables(conn)
    finally:
        conn.close()


@pytest.fixture()
def prod_form():
    """audit_events в прод-форме (с created_at) + дубли с ПЕРЕВЁРНУТЫМ порядком вставки."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — reconciling audit requires live PG; "
            "issue: P1-C · expiry: 2026-12-31"
        )
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        _reset_tables(conn)
        cur = conn.cursor()
        cur.execute(PROD_FORM_SCHEMA)
        # Дубль по ключу (tenant_id, event_type, entity_id): поздняя строка вставлена ПЕРВОЙ.
        cur.execute(
            "INSERT INTO audit_events (id,tenant_id,event_type,entity_type,entity_id,data,created_at)"
            " VALUES ('later','tA','job.user_confirmed','job','job-A','{}'::jsonb,'2026-09-21T10:00:00Z')"
        )
        cur.execute(
            "INSERT INTO audit_events (id,tenant_id,event_type,entity_type,entity_id,data,created_at)"
            " VALUES ('earlier','tA','job.user_confirmed','job','job-A','{}'::jsonb,'2026-09-20T10:00:00Z')"
        )
        yield conn
    finally:
        _cleanup(conn)


@pytest.fixture()
def clean_form():
    """audit_events в прод-форме БЕЗ дублей — база для «повторный накат = noop»."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — reconciling audit requires live PG; "
            "issue: P1-C · expiry: 2026-12-31"
        )
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        _reset_tables(conn)
        cur = conn.cursor()
        cur.execute(PROD_FORM_SCHEMA)
        cur.execute(
            "INSERT INTO audit_events (id,tenant_id,event_type,entity_type,entity_id,data,created_at)"
            " VALUES ('a1','tA','job.user_confirmed','job','job-A','{}'::jsonb,'2026-09-20T10:00:00Z')"
        )
        yield conn
    finally:
        _cleanup(conn)


@pytest.mark.pg
def test_012_duplicates_are_refused_and_nothing_deleted(prod_form):
    """Дубли по ключу → 012 отказывает (fail-closed) и НЕ удаляет ни одной строки."""
    conn = prod_form
    with pytest.raises(Exception) as exc:
        _apply_012(conn)
    assert "дублей" in str(exc.value) or "duplicate" in str(exc.value).lower()

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    assert cur.fetchone()[0] == 2  # обе строки на месте
    cur.execute("SELECT id FROM audit_events ORDER BY created_at")
    assert [r[0] for r in cur.fetchall()] == ["earlier", "later"]
    cur.execute("SELECT to_regclass('public.audit_events_dedupe_backup_full')")
    assert cur.fetchone()[0] is None  # отказали до резерва — backup не создан, потерь нет


@pytest.mark.pg
def test_012_reapply_is_noop(clean_form):
    """Повторный накат 012 на чистой таблице — rc=0, 0 изменений, backup не растёт."""
    conn = clean_form
    _apply_012(conn)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    n_before = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM audit_events_dedupe_backup_full")
    b_before = cur.fetchone()[0]
    assert n_before == 1 and b_before == 0

    _apply_012(conn)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    assert cur.fetchone()[0] == n_before
    cur.execute("SELECT count(*) FROM audit_events_dedupe_backup_full")
    assert cur.fetchone()[0] == b_before


@pytest.mark.pg
def test_012_ignores_identity_column():
    """Авто-колонка (GENERATED ALWAYS AS IDENTITY) не вставляется явно — 012 не падает."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — reconciling audit requires live PG; "
            "issue: P1-C · expiry: 2026-12-31"
        )
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        _reset_tables(conn)
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE audit_events (id TEXT PRIMARY KEY, tenant_id TEXT,"
            " event_type TEXT, entity_type TEXT, entity_id TEXT, data JSONB,"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
            " seq BIGINT GENERATED ALWAYS AS IDENTITY)"
        )
        cur.execute(
            "INSERT INTO audit_events (id,tenant_id,event_type,entity_type,entity_id,data,created_at)"
            " VALUES ('a','tA','job.user_confirmed','job','job-A','{}'::jsonb,'2026-09-20T10:00:00Z')"
        )
        _apply_012(conn)  # не падает: identity-колонка не вставляется явно

        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM audit_events")
        assert cur.fetchone()[0] == 1
    finally:
        _cleanup(conn)


@pytest.mark.pg
def test_012_no_dups_unknown_and_null_variants():
    """Нет дублей, но есть entity_id='unknown' и NULL-варианты → 012 rc=0, 0 изменений, без отказа.

    Предикат дедупа исключает unknown и NULL-ключи (согласовано с частичным UNIQUE),
    поэтому «грязный» по ключам набор не трогается.
    """
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — reconciling audit requires live PG; "
            "issue: P1-C · expiry: 2026-12-31"
        )
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())
    try:
        _reset_tables(conn)
        cur = conn.cursor()
        cur.execute(PROD_FORM_SCHEMA)
        rows = [
            # (id, tenant_id, event_type, entity_type, entity_id, created_at)
            ("u1", "tA", "job.user_confirmed", "job", "unknown", "2026-09-20T10:00:00Z"),
            ("n1", None, "job.user_confirmed", "job", "job-N", "2026-09-20T10:00:00Z"),
            ("n2", "tA", None, "job", "job-M", "2026-09-20T10:00:00Z"),
            ("n3", "tA", "job.user_confirmed", "job", None, "2026-09-20T10:00:00Z"),
            ("a1", "tA", "job.user_confirmed", "job", "job-A", "2026-09-20T10:00:00Z"),
        ]
        for rid, tid, et, ent, eid, ts in rows:
            cur.execute(
                "INSERT INTO audit_events (id,tenant_id,event_type,entity_type,entity_id,data,created_at)"
                " VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,%s)",
                (rid, tid, et, ent, eid, ts),
            )
        _apply_012(conn)  # rc=0, без отказа

        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM audit_events")
        assert cur.fetchone()[0] == 5  # 0 изменений — все строки живы
        cur.execute("SELECT count(*) FROM audit_events_dedupe_backup_full")
        assert cur.fetchone()[0] == 0  # backup пуст
    finally:
        _cleanup(conn)
