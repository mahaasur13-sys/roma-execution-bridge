"""G-AUDIT-DDL-DRIFT (R2b/R2c/R2e): migrations/010a_audit_events_legacy_reconcile.sql.

Проверяет реконсиляцию ИСТОРИЧЕСКОЙ формы `audit_events` (рантайм-бутстрап,
docs/DECISIONOS-TRANSFORMATION.md §2.1: `id UUID DEFAULT gen_random_uuid()`,
`entity_id UUID NOT NULL`, `data JSONB`) ДО 011 — иначе 011 падает fail-closed на
сверке сигнатуры и цепочка миграций встаёт целиком.

Контракт файла 010a:
  * чистая БД (таблицы нет) — no-op, канон создаёт 011;
  * историческая uuid-схема — конвертация uuid -> text без потери значений,
    NOT NULL сохраняется, uuid-DEFAULT становится text-safe;
  * `data` text/json -> jsonb с проверкой валидности JSON до ALTER;
  * повторный прогон — 0 изменений;
  * неожиданная схема (тип вне allowlist, отсутствующая базовая колонка,
    лишняя NOT NULL-без-default колонка, PK не ровно (id)) — отказ ДО мутации.

011 и 012 НЕ редактируются: их текст заморожен (проверяется офлайн-тестом ниже).
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_010A = REPO_ROOT / "migrations" / "010a_audit_events_legacy_reconcile.sql"
MIGRATION_011 = REPO_ROOT / "migrations" / "011_audit_events.sql"
MIGRATION_012 = REPO_ROOT / "migrations" / "012_audit_events_reconcile_order.sql"

# Замороженные (уже применённые на проде) файлы: их текст меняться не должен.
FROZEN_SHA256 = {
    "011_audit_events.sql": "975ddb60099c0e64b52f6acb9c906c6bcf7460ce72847a31d910185a3de27e0d",
    "012_audit_events_reconcile_order.sql": "fcb6b0844346fad8320240613ac8c9bbab91cea753fc9e6a13e4115445293b0c",
}

BASE_COLUMNS = ("id", "tenant_id", "event_type", "entity_type", "entity_id", "data")

# Историческая форма рантайм-бутстрапа (docs/DECISIONOS-TRANSFORMATION.md §2.1).
LEGACY_UUID_SCHEMA = """
CREATE TABLE audit_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   UUID NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
)
"""

# Вариант с TEXT-данными (зеркало SQLite-бутстрапа db_adapter._ensure_audit_events_table).
LEGACY_TEXT_DATA_SCHEMA = """
CREATE TABLE audit_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    event_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    data        TEXT
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


def _apply(conn, path: Path) -> None:
    """Накат тела миграции целиком, как это делает scripts/run_migrations.py."""
    body = path.read_text()
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(body)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _reset(conn) -> None:
    conn.autocommit = True
    cur = conn.cursor()
    for table in (
        "audit_events",
        "audit_events_dedupe_backup",
        "audit_events_dedupe_backup_full",
    ):
        cur.execute(f"DROP TABLE IF EXISTS {table}")


def _types(conn) -> dict[str, str]:
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name, udt_name FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name='audit_events'"
    )
    return dict(cur.fetchall())


def _column_default(conn, column: str) -> str | None:
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT pg_get_expr(d.adbin, d.adrelid)"
        "  FROM pg_attribute a"
        "  JOIN pg_class c ON c.oid = a.attrelid"
        "  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
        " WHERE c.oid = 'public.audit_events'::regclass AND a.attname = %s AND a.attnum > 0",
        (column,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _is_nullable(conn, column: str) -> str | None:
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_schema='public' AND table_name='audit_events' AND column_name=%s",
        (column,),
    )
    row = cur.fetchone()
    return row[0] if row else None


@pytest.fixture()
def legacy_db():
    """Изолированная схема audit_events под тест; PG берётся из PG_DSN (тестовая БД)."""
    if not _pg_reachable():
        pytest.skip(
            "PG not reachable — reconciling audit requires live PG; issue: G-AUDIT-DDL-DRIFT · expiry: 2026-12-31"
        )
    import psycopg2

    conn = psycopg2.connect(_pg_dsn())

    def _make(schema_sql: str, rows: list[tuple] | None = None):
        _reset(conn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(schema_sql)
        for row in rows or []:
            cur.execute(
                "INSERT INTO audit_events (id, tenant_id, event_type, entity_type, entity_id, data)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                row,
            )

    _make.conn = conn  # type: ignore[attr-defined]
    _make.reset = _reset  # type: ignore[attr-defined]
    try:
        yield _make
    finally:
        try:
            _reset(conn)
        finally:
            conn.close()


# --------------------------------------------------------------------------
# R2e — воспроизводимость: чистая БД, повторный прогон, отказ на чужой схеме
# --------------------------------------------------------------------------


@pytest.mark.pg
def test_010a_is_noop_when_table_absent_and_011_creates_canonical(legacy_db):
    """Чистая БД: 010a ничего не создаёт, канон (data JSONB) создаёт 011."""
    conn = legacy_db.conn
    legacy_db("SELECT 1")

    _apply(conn, MIGRATION_010A)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.audit_events')")
    assert cur.fetchone()[0] is None, "010a не должен создавать таблицу на чистой БД"

    _apply(conn, MIGRATION_011)
    assert _types(conn) == {
        "id": "text",
        "tenant_id": "text",
        "event_type": "text",
        "entity_type": "text",
        "entity_id": "text",
        "data": "jsonb",
    }


@pytest.mark.pg
def test_010a_reconciles_legacy_uuid_schema_losslessly(legacy_db):
    """uuid -> text: значения сохранены, NOT NULL и PK (id) сохранены, строки целы."""
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    ent_a, ent_b = uuid.uuid4(), uuid.uuid4()
    legacy_db(
        LEGACY_UUID_SCHEMA,
        [
            (str(id_a), "tA", "job.user_confirmed", "job", str(ent_a), '{"user_confirmed": true}'),
            (str(id_b), "tB", "job.user_confirmed", "job", str(ent_b), '{"user_confirmed": true}'),
        ],
    )
    conn = legacy_db.conn
    before = _types(conn)
    assert before["id"] == "uuid" and before["entity_id"] == "uuid"

    _apply(conn, MIGRATION_010A)

    assert _types(conn) == {
        "id": "text",
        "tenant_id": "text",
        "event_type": "text",
        "entity_type": "text",
        "entity_id": "text",
        "data": "jsonb",  # исторический JSONB остаётся jsonb, без перезаписи
        "created_at": "timestamptz",  # доп. колонка не тронута
    }
    assert _is_nullable(conn, "entity_id") == "NO", "NOT NULL обязан сохраниться"

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    assert cur.fetchone()[0] == 2, "число строк не изменилось"
    cur.execute("SELECT id, entity_id, data FROM audit_events ORDER BY id")
    rows = cur.fetchall()
    assert {r[0] for r in rows} == {str(id_a), str(id_b)}, "uuid сохранён в каноническом виде"
    assert {r[1] for r in rows} == {str(ent_a), str(ent_b)}
    assert all(r[2] == {"user_confirmed": True} for r in rows)

    # PK по (id) сохранён и продолжает работать.
    with pytest.raises(Exception):
        conn.autocommit = False
        try:
            cur.execute(
                "INSERT INTO audit_events (id, tenant_id, event_type, entity_type, entity_id, data)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (str(id_a), "tA", "dup", "job", "e", "{}"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@pytest.mark.pg
def test_010a_restores_text_safe_default(legacy_db):
    """uuid-DEFAULT `gen_random_uuid()` становится `gen_random_uuid()::text` и работает."""
    legacy_db(LEGACY_UUID_SCHEMA, [])
    conn = legacy_db.conn
    assert "gen_random_uuid" in (_column_default(conn, "id") or "")

    _apply(conn, MIGRATION_010A)

    default = _column_default(conn, "id")
    assert default is not None and "::text" in default, f"дефолт не text-safe: {default}"

    conn.autocommit = True
    cur = conn.cursor()
    # INSERT без id: дефолт обязан сработать в text-колонке (иначе uuid -> text ошибка).
    cur.execute(
        "INSERT INTO audit_events (tenant_id, event_type, entity_type, entity_id, data)"
        " VALUES ('tA','ev','job','unknown','{}') RETURNING id"
    )
    new_id = cur.fetchone()[0]
    assert isinstance(new_id, str) and len(new_id) == 36
    uuid.UUID(new_id)  # канонический uuid-текст


@pytest.mark.pg
def test_010a_is_idempotent_on_second_run(legacy_db):
    """Повторный прогон — 0 изменений: типы, дефолт и строки те же."""
    ent = uuid.uuid4()
    legacy_db(
        LEGACY_UUID_SCHEMA,
        [(str(uuid.uuid4()), "tA", "ev", "job", str(ent), '{"a": 1}')],
    )
    conn = legacy_db.conn

    _apply(conn, MIGRATION_010A)
    types_once = _types(conn)
    default_once = _column_default(conn, "id")

    _apply(conn, MIGRATION_010A)  # второй прогон обязан быть no-op

    assert _types(conn) == types_once
    assert _column_default(conn, "id") == default_once
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    assert cur.fetchone()[0] == 1


@pytest.mark.pg
def test_010a_converts_text_data_to_jsonb(legacy_db):
    """R2b: `data TEXT` с валидным JSON конвертируется в jsonb без потери значений."""
    legacy_db(
        LEGACY_TEXT_DATA_SCHEMA,
        [
            ("a1", "tA", "ev", "job", "job-A", '{"k": 1}'),
            ("a2", "tA", "ev", "job", "job-B", "[]"),
            ("a3", "tA", "ev", "job", "job-C", None),
        ],
    )
    conn = legacy_db.conn

    _apply(conn, MIGRATION_010A)

    assert _types(conn)["data"] == "jsonb"
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT data FROM audit_events WHERE id='a1'")
    assert cur.fetchone()[0] == {"k": 1}
    cur.execute("SELECT data FROM audit_events WHERE id='a2'")
    assert cur.fetchone()[0] == []
    cur.execute("SELECT data FROM audit_events WHERE id='a3'")
    assert cur.fetchone()[0] is None, "NULL обязан остаться NULL (не 'null'::jsonb)"


@pytest.mark.pg
def test_010a_refuses_invalid_json_and_leaves_schema_unchanged(legacy_db):
    """Невалидный JSON в data: отказ ДО ALTER, схема не изменена."""
    legacy_db(
        LEGACY_TEXT_DATA_SCHEMA,
        [("a1", "tA", "ev", "job", "job-A", "not-json")],
    )
    conn = legacy_db.conn

    with pytest.raises(Exception, match="невалидный JSON"):
        _apply(conn, MIGRATION_010A)

    assert _types(conn)["data"] == "text", "тип не должен меняться при отказе"


@pytest.mark.pg
def test_010a_refuses_unexpected_column_type_before_mutation(legacy_db):
    """Тип вне allowlist (int4) — отказ, схема остаётся как была."""
    legacy_db(
        "CREATE TABLE audit_events ("
        " id INTEGER PRIMARY KEY, tenant_id TEXT, event_type TEXT,"
        " entity_type TEXT, entity_id TEXT, data JSONB)",
        [],
    )
    conn = legacy_db.conn

    with pytest.raises(Exception, match="неожиданный тип"):
        _apply(conn, MIGRATION_010A)

    assert _types(conn)["id"] == "int4"


@pytest.mark.pg
def test_010a_refuses_missing_base_column(legacy_db):
    """Нет базовой колонки (entity_type) — отказ до мутации."""
    legacy_db(
        "CREATE TABLE audit_events ( id TEXT PRIMARY KEY, tenant_id TEXT, event_type TEXT, entity_id TEXT, data JSONB)",
        [],
    )
    conn = legacy_db.conn

    with pytest.raises(Exception, match="отсутствуют базовые колонки"):
        _apply(conn, MIGRATION_010A)


@pytest.mark.pg
def test_010a_refuses_extra_required_column_before_mutation(legacy_db):
    """Лишняя NOT NULL-без-default колонка ломает INSERT db_adapter — отказ до мутации."""
    legacy_db(
        "CREATE TABLE audit_events ("
        " id TEXT PRIMARY KEY, tenant_id TEXT, event_type TEXT,"
        " entity_type TEXT, entity_id TEXT, data JSONB,"
        " request_id TEXT NOT NULL)",
        [],
    )
    conn = legacy_db.conn

    with pytest.raises(Exception, match="обязательные дополнительные колонки"):
        _apply(conn, MIGRATION_010A)


@pytest.mark.pg
def test_010a_unblocks_011_on_legacy_uuid_schema(legacy_db):
    """R2e: после 010a канонический 011 отрабатывает — дедуп, backup и частичный UNIQUE."""
    ent = uuid.uuid4()
    legacy_db(
        LEGACY_UUID_SCHEMA,
        [
            (str(uuid.uuid4()), "tA", "job.user_confirmed", "job", str(ent), '{"v": 1}'),
            (str(uuid.uuid4()), "tA", "job.user_confirmed", "job", str(ent), '{"v": 1}'),
        ],
    )
    conn = legacy_db.conn

    # Без 010a 011 обязан отказать (дрейф сигнатуры) — это и есть исходный дефект.
    with pytest.raises(Exception, match="schema drift"):
        _apply(conn, MIGRATION_011)

    _apply(conn, MIGRATION_010A)
    _apply(conn, MIGRATION_011)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events WHERE entity_id = %s", (str(ent),))
    assert cur.fetchone()[0] == 1, "дубль должен быть снят 011"
    cur.execute("SELECT count(*) FROM audit_events_dedupe_backup")
    assert cur.fetchone()[0] == 1, "снятый дубль обязан быть в backup"
    cur.execute(
        "SELECT indexname FROM pg_indexes"
        " WHERE schemaname='public' AND tablename='audit_events'"
        "   AND indexname='audit_events_dedupe_uidx'"
    )
    assert cur.fetchone() is not None, "частичный UNIQUE обязан появиться"


@pytest.mark.pg
def test_010a_011_012_chain_is_reproducible(legacy_db):
    """Полная цепочка 010a -> 011 -> 012 воспроизводима: второй проход — no-op."""
    legacy_db(LEGACY_UUID_SCHEMA, [(str(uuid.uuid4()), "tA", "ev", "job", str(uuid.uuid4()), "{}")])
    conn = legacy_db.conn

    for _ in range(2):
        _apply(conn, MIGRATION_010A)
        _apply(conn, MIGRATION_011)
        _apply(conn, MIGRATION_012)

    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM audit_events")
    assert cur.fetchone()[0] == 1


# --------------------------------------------------------------------------
# Офлайн-инварианты: порядок наката и заморозка уже применённых 011/012
# --------------------------------------------------------------------------


def test_010a_sorts_between_010_and_011():
    """Раннер накатывает по имени: 010a обязан идти после 010 и ДО 011."""
    names = sorted(p.name for p in (REPO_ROOT / "migrations").glob("*.sql"))
    idx = names.index(MIGRATION_010A.name)
    assert names[idx - 1] == "010_workers_capabilities.sql"
    assert names[idx + 1] == "011_audit_events.sql"


def test_011_and_012_are_frozen():
    """Применённые 011/012 не редактируются — их текст зафиксирован хешем."""
    for name, expected in FROZEN_SHA256.items():
        digest = hashlib.sha256((REPO_ROOT / "migrations" / name).read_bytes()).hexdigest()
        assert digest == expected, f"{name} изменён — применённые миграции не редактируются"


def test_010a_does_not_touch_other_tables():
    """010a не создаёт таблиц и не трогает ничего, кроме audit_events."""
    body = MIGRATION_010A.read_text()
    for forbidden in ("usage_events", "ledger_entries", "tenants", "CREATE TABLE", "DROP TABLE"):
        assert forbidden not in body, f"010a не должен упоминать {forbidden}"
