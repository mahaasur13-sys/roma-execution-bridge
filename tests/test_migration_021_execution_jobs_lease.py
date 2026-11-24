"""021a: DDL аренды задачи воркером — аддитивность, идемпотентность, unbound-строки.

Контракт миграции ``021_execution_jobs_lease.sql`` (только DDL, без логики аренды):

  * колонки добавляются через ``ADD COLUMN IF NOT EXISTS`` — повторный прогон no-op;
  * никаких индексов, CHECK и backfill — код эти колонки пока не читает (021b);
  * строки, жившие до 021 (в т.ч. ``running``), остаются ``unbound``:
    ``worker_id`` и ``lease_token_hash`` — NULL, ``lease_epoch`` — 0, ``status`` не меняется;
  * SQLite-зеркало ``_ensure_execution_jobs_table`` создаёт те же колонки как на
    новой БД, так и через guarded ALTER на таблице, созданной до 021.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPO_ROOT / "migrations" / "021_execution_jobs_lease.sql"
LEASE_COLUMNS = ("worker_id", "lease_token_hash", "lease_epoch", "lease_expires_at")
LEASE_ROW = ("running", None, None, 0, None)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _ddl() -> str:
    """Тело миграции без комментариев: проверяем ровно исполняемые операторы."""
    return "\n".join(line for line in _sql().splitlines() if not line.strip().startswith("--"))


def _connect_test_db():
    """A1 на уровне теста: fail-closed до любого DDL, только тестовая БД.

    PG-тесты ниже выполняют DDL (``DROP COLUMN`` / ``ADD COLUMN``) в транзакции
    с ``rollback()``; откат не защищает от блокировок на чужой базе. Сессионная
    изоляция (``tests/conftest.py`` → ``_isolation.resolve_or_fail``) уже
    отказывает прогону с DSN не на ``roma_test``; здесь та же проверка
    повторяется перед подключением — на случай обхода conftest
    (``--noconftest``, прямой вызов) или подмены ``PG_DSN`` после старта сессии.
    Критерий — тот же, что у A1: точное имя БД из ``_isolation.ALLOWED_DB_NAMES``
    (``roma_test``, как в CI). После подключения сверяется ``current_database()``.
    Отказ — ``pytest.fail`` (не skip): обход изоляции не должен выглядеть зелёным.
    """
    import _isolation
    import psycopg2

    dsn = (os.environ.get("PG_DSN") or "").strip()
    if not dsn:
        pytest.fail(
            f"{_isolation.REFUSED_MARKER}: PG_DSN пуст — PG-тест 021 без тестовой БД не подключается", pytrace=False
        )
    dbname = _isolation.identity(dsn)[2]
    if dbname not in _isolation.ALLOWED_DB_NAMES:
        pytest.fail(
            f"{_isolation.REFUSED_MARKER}: PG_DSN ведёт в {_isolation.describe(dsn)}; "
            f"разрешены только {sorted(_isolation.ALLOWED_DB_NAMES)} — DDL теста 021 не выполняется",
            pytrace=False,
        )

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_database()")
        actual = cur.fetchone()[0]
    except BaseException:
        conn.close()
        raise
    if actual not in _isolation.ALLOWED_DB_NAMES:
        conn.close()
        pytest.fail(
            f"{_isolation.REFUSED_MARKER}: подключение ушло в БД {actual!r}, "
            f"разрешены только {sorted(_isolation.ALLOWED_DB_NAMES)} — DDL теста 021 не выполняется",
            pytrace=False,
        )
    return conn


def test_021_ddl_is_additive_and_follows_020() -> None:
    """(1) Только ADD COLUMN IF NOT EXISTS; файл 021 идёт сразу после 020.

    «Порядок» здесь — место файла в последовательности миграций
    (``migrations/*.sql`` по имени: 021 следует сразу за
    ``020_host_agent_tokens.sql``). Порядок колонок внутри 021 этот тест
    не проверяет.
    """
    upper = _ddl().upper()
    for column in LEASE_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column.upper()}" in upper, f"021 не добавляет {column}"
    assert "CREATE INDEX" not in upper, "021 не создаёт индексов — блокировки отдельным шагом"
    assert "CHECK" not in upper, "021 не ставит CHECK: контракт колонок ещё не зафиксирован"
    assert "DROP" not in upper, "021 обязана быть чисто аддитивной"
    assert "UPDATE " not in upper, "021 не делает backfill старых строк"
    assert "INTERVAL" not in upper, "021 не выставляет срок жизни аренды — это 021b"

    assert MIGRATION.name.endswith(".sql") and MIGRATION.name.startswith("021_")
    names = sorted(p.name for p in (REPO_ROOT / "migrations").glob("*.sql"))
    assert names.index(MIGRATION.name) == names.index("020_host_agent_tokens.sql") + 1


def test_sqlite_mirror_matches_021() -> None:
    """(2) SQLite-зеркало: новая таблица и guarded ALTER на таблице до 021."""
    conn = sqlite3.connect(":memory:")
    try:
        c = conn.cursor()
        db_adapter._ensure_execution_jobs_table(c)
        c.execute("INSERT INTO execution_jobs (id, tenant_id, status) VALUES ('j1', 'test-t', 'running')")
        fresh = c.execute(
            "SELECT status, worker_id, lease_token_hash, lease_epoch, lease_expires_at "
            "FROM execution_jobs WHERE id = 'j1'"
        ).fetchone()
        assert fresh == LEASE_ROW, f"новая таблица в SQLite отдала {fresh}"

        db_adapter._ensure_execution_jobs_table(c)  # повторный вызов — no-op
    finally:
        conn.close()

    legacy = sqlite3.connect(":memory:")
    try:
        c = legacy.cursor()
        c.execute(
            """
            CREATE TABLE execution_jobs (
                id               TEXT PRIMARY KEY,
                decision_id      TEXT,
                tenant_id        TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                payload          TEXT DEFAULT '{}',
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now')),
                cost_usd         REAL NOT NULL DEFAULT 0.0,
                backend          TEXT,
                backend_job_id   TEXT,
                duration_seconds REAL DEFAULT 0.0,
                started_at       TEXT,
                completed_at     TEXT,
                error            TEXT
            )
            """
        )
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, started_at) VALUES ('j2', 'test-t', 'running', '2026-11-10 00:00:00')"
        )

        db_adapter._ensure_execution_jobs_table(c)

        altered = c.execute(
            "SELECT status, worker_id, lease_token_hash, lease_epoch, lease_expires_at "
            "FROM execution_jobs WHERE id = 'j2'"
        ).fetchone()
        assert altered == LEASE_ROW, f"guarded ALTER в SQLite отдал {altered}"
    finally:
        legacy.close()


@pytest.mark.pg
def test_migration_021_idempotent() -> None:
    """(3) Повторное применение 021 в одной транзакции не падает и типы не дрейфуют."""
    sql = _sql()
    conn = _connect_test_db()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cur.execute(sql)

        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'execution_jobs'
              AND column_name = ANY(%s)
            """,
            (list(LEASE_COLUMNS),),
        )
        found = {row[0]: row[1:] for row in cur.fetchall()}
        assert set(found) == set(LEASE_COLUMNS), f"021 добавила не все колонки: {sorted(found)}"

        assert found["worker_id"][1] == "YES", "worker_id обязан быть nullable (unbound возможен)"
        assert found["lease_token_hash"][1] == "YES", "lease_token_hash обязан быть nullable"
        assert found["lease_expires_at"][1] == "YES", "lease_expires_at обязан быть nullable"
        assert found["lease_epoch"][1] == "NO", "lease_epoch обязан быть NOT NULL"
        assert found["lease_epoch"][2] == "0", f"lease_epoch default должен быть 0, а не {found['lease_epoch'][2]}"
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.pg
def test_migration_021_existing_running_rows_unbound() -> None:
    """(4) ``running``-строка, жившая до 021, остаётся unbound и со своим статусом.

    В одной транзакции: снимаем колонки (состояние «до 021»), вставляем running,
    применяем 021 и сверяем. Всё откатывается — схема roma_test не меняется.
    """
    sql = _sql()
    conn = _connect_test_db()
    job_id = "test-migration-021-running"
    try:
        cur = conn.cursor()
        cur.execute("ALTER TABLE execution_jobs " + ", ".join(f"DROP COLUMN IF EXISTS {c}" for c in LEASE_COLUMNS))
        cur.execute(
            """
            INSERT INTO execution_jobs (id, decision_id, tenant_id, status, payload, started_at)
            VALUES (%s, NULL, %s, 'running', '{}'::jsonb, now())
            """,
            (job_id, "test-tenant-021"),
        )

        cur.execute(sql)

        cur.execute(
            """
            SELECT status, worker_id, lease_token_hash, lease_epoch, lease_expires_at, started_at IS NOT NULL
            FROM execution_jobs WHERE id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
        assert row[:5] == LEASE_ROW, f"021 изменила старую running-строку: {row[:5]}"
        assert row[5] is True, "021 не должна терять started_at"
    finally:
        conn.rollback()
        conn.close()
