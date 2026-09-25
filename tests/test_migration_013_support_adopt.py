"""Q-B4: migrations/013_support_chat_adopt.sql — усыновление схемы support_chat раннером.

Класс дефекта: объекты модуля support_chat жили вне книги миграций (scheme давал
create_all). Проверяем adoption-файл: создаёт недостающее, повтор = no-op,
существующие данные не трогает, разрушающих операторов нет.

Файл-источник support_chat/migrations/004_support_chat.sql не удаляется и не
редактируется — этот тест его не касается.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_013 = REPO_ROOT / "migrations" / "013_support_chat_adopt.sql"

SUPPORT_TABLES = (
    "support_tickets",
    "support_messages",
    "support_participants",
    "support_attachments",
    "support_csat_ratings",
)
SUPPORT_TYPES = ("ticket_status", "ticket_priority", "participant_role", "message_type")
SUPPORT_INDEXES = (
    "idx_support_tickets_tenant",
    "idx_support_tickets_status",
    "idx_support_tickets_assigned",
    "idx_support_messages_ticket",
    "idx_support_messages_created",
    "idx_support_participants_ticket",
    "idx_support_attachments_ticket",
    "idx_support_csat_ticket",
)


def _pg_dsn() -> str | None:
    return os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")


def _pg_reachable() -> bool:
    dsn = _pg_dsn()
    if not dsn:
        return False
    try:
        import psycopg2

        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


def _require_test_dsn() -> str:
    """Страховка от DROP TABLE по прод-БД (урок Z-2a): только тестовый DSN."""
    dsn = _pg_dsn() or ""
    if "roma_test" not in dsn and os.environ.get("ROMA_TEST_DSN") != "1":
        pytest.fail(
            "отказ: не тестовый DSN. Тест дропает таблицы/типы support_* — нужен DSN, "
            "содержащий 'roma_test', либо ROMA_TEST_DSN=1. Прод-инстанс не трогаем."
        )
    return dsn


def _read_migration() -> str:
    return MIGRATION_013.read_text(encoding="utf-8")


def _apply_013(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_read_migration())
    conn.commit()


def _reset_objects(conn) -> None:
    """Снести объекты adoption-миграции (только на тестовом DSN)."""
    _require_test_dsn()
    with conn.cursor() as cur:
        for table in SUPPORT_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        cur.execute("DELETE FROM schema_migrations WHERE filename = %s", ("013_support_chat_adopt.sql",))
        for type_name in SUPPORT_TYPES:
            cur.execute(f"DROP TYPE IF EXISTS {type_name} CASCADE")
    conn.commit()


def _count_objects(conn) -> tuple[int, int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (list(SUPPORT_TABLES),),
        )
        tables = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND indexname = ANY(%s)",
            (list(SUPPORT_INDEXES),),
        )
        indexes = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_type WHERE typname = ANY(%s)", (list(SUPPORT_TYPES),))
        types = cur.fetchone()[0]
    return tables, indexes, types


def _objects_exist(conn) -> bool:
    return _count_objects(conn) == (len(SUPPORT_TABLES), len(SUPPORT_INDEXES), len(SUPPORT_TYPES))


def _executable_sql() -> str:
    """SQL без комментариев: проверяем операторы, а не упоминания в тексте шапки.

    Референциальные действия (`ON DELETE CASCADE` / `ON UPDATE ...`) — часть
    определения FK, а не разрушающий DML: перед проверкой они вырезаются
    (иначе гейт ложно-красный на законном `ON DELETE CASCADE`).
    """
    lines = []
    for raw in _read_migration().splitlines():
        line = raw.split("--", 1)[0]
        if line.strip():
            lines.append(line)
    sql = "\n".join(lines).upper()
    return re.sub(r"ON (DELETE|UPDATE)\s+\w+(\s+\w+)?", "", sql)


def test_013_source_is_additive_only() -> None:
    """Разрушающих операторов в adoption-файле нет (гейт промта ZO-4)."""
    sql = _executable_sql()
    for banned in ("DROP ", "DELETE ", "UPDATE ", "TRUNCATE "):
        assert banned not in sql, f"запрещённый оператор {banned!r} в {MIGRATION_013.name}"
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "IF NOT EXISTS" in sql


@pytest.mark.pg
def test_013_creates_objects_when_absent() -> None:
    if not _pg_reachable():
        pytest.skip("PostgreSQL DSN не задан или недоступен")
    import psycopg2

    conn = psycopg2.connect(_require_test_dsn())
    try:
        _reset_objects(conn)
        assert not _objects_exist(conn), "предусловие: объекты support_* должны отсутствовать"
        _apply_013(conn)
        assert _count_objects(conn) == (len(SUPPORT_TABLES), len(SUPPORT_INDEXES), len(SUPPORT_TYPES))
    finally:
        conn.close()


@pytest.mark.pg
def test_013_reapply_is_noop() -> None:
    """Повторный накат = 0 изменений, rc=0 (идемпотентность)."""
    if not _pg_reachable():
        pytest.skip("PostgreSQL DSN не задан или недоступен")
    import psycopg2

    conn = psycopg2.connect(_require_test_dsn())
    try:
        _reset_objects(conn)
        _apply_013(conn)
        before = _count_objects(conn)
        _apply_013(conn)  # повтор: не должно быть ни ошибки, ни изменений
        assert _count_objects(conn) == before
    finally:
        conn.close()


@pytest.mark.pg
def test_013_preserves_existing_rows() -> None:
    """Усыновление существующей схемы: данные и идентификаторы не теряются."""
    if not _pg_reachable():
        pytest.skip("PostgreSQL DSN не задан или недоступен")
    import psycopg2

    conn = psycopg2.connect(_require_test_dsn())
    try:
        _reset_objects(conn)
        _apply_013(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO support_tickets (tenant_id, subject, body, created_by) "
                "VALUES ('qb4-adopt', 'тема', 'тело', 'qb4') RETURNING ticket_id"
            )
            ticket_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO support_messages (ticket_id, sender_id, sender_role, body) "
                "VALUES (%s, 'qb4', 'tenant_user', 'привет')",
                (ticket_id,),
            )
        conn.commit()

        _apply_013(conn)  # adoption поверх данных

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM support_tickets WHERE tenant_id = 'qb4-adopt'")
            tickets = cur.fetchone()[0]
            cur.execute("SELECT ticket_id::text FROM support_tickets WHERE ticket_id = %s", (ticket_id,))
            row = cur.fetchone()
            cur.execute("SELECT count(*) FROM support_messages WHERE ticket_id = %s", (ticket_id,))
            messages = cur.fetchone()[0]
        assert tickets == 1, "строка тикета должна остаться"
        assert row is not None and row[0] == str(ticket_id), "ticket_id должен сохраниться"
        assert messages == 1, "сообщение должно остаться"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM support_tickets WHERE tenant_id = 'qb4-adopt'")
        conn.commit()
        conn.close()
