"""L1 append-only ledger — триггер 009_ledger_append_only_trigger.sql.

Проверяет, что ledger_entries принимает только INSERT, а UPDATE/DELETE/TRUNCATE
падают. Требует живой PG (PG_DSN); без него тесты skip — SQLite-путь триггеров
не имеет, и это осознанно (SQLite не является продовым хранилищем ledger).

Запуск:
    PG_DSN=postgresql://... .venv/bin/python -m pytest tests/test_ledger_append_only_trigger.py -q
"""
import os

import pytest

TRIGGER_ROW = "ledger_entries_immutable"
TRIGGER_TRUNCATE = "ledger_entries_no_truncate"
FUNCTION_NAME = "ledger_no_mutate"


def _pg_conn():
    """Живое PG-соединение или skip (нет PG_DSN / PG недоступен)."""
    if not os.environ.get("PG_DSN"):
        pytest.skip("PG_DSN не задан — триггер L1 проверяется только на живом PG; issue: P1-C · expiry: 2026-12-31")

    from billing.pg_connection import get_pg_manager, PGUnavailableError
    try:
        ctx = get_pg_manager().get_connection("test_ledger_append_only")
    except PGUnavailableError:
        pytest.skip("PG недоступен в этом процессе pytest; issue: P1-C · expiry: 2026-12-31")
    return ctx


def _seed_entry(cur):
    """Гарантирует наличие строки для UPDATE/DELETE-проверок. INSERT разрешён."""
    cur.execute(
        "INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)"
        " VALUES (%s, %s, 'CREDIT', 0.0, 'USD', '{}'::jsonb)"
        " ON CONFLICT (ledger_id) DO NOTHING RETURNING ledger_id",
        (f"l1-trigger-test-{os.getpid()}", "test-l1-trigger"),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "SELECT ledger_id FROM ledger_entries WHERE tenant_id = %s LIMIT 1",
        ("test-l1-trigger",),
    )
    fallback = cur.fetchone()
    if not fallback:
        pytest.skip("ledger_entries пуста и INSERT недоступен — нечего проверять; issue: P1-C · expiry: 2026-12-31")
    return fallback[0]


def test_trigger_names_present():
    """После 009 на ledger_entries висят оба триггера и функция ledger_no_mutate."""
    with _pg_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT t.tgname, p.proname, t.tgenabled"
            " FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid"
            " WHERE t.tgrelid = 'ledger_entries'::regclass AND NOT t.tgisinternal",
        )
        found = {name: (fn, enabled) for name, fn, enabled in cur.fetchall()}

    assert TRIGGER_ROW in found, f"нет триггера {TRIGGER_ROW}: {sorted(found)}"
    assert TRIGGER_TRUNCATE in found, f"нет триггера {TRIGGER_TRUNCATE}: {sorted(found)}"
    assert found[TRIGGER_ROW][0] == FUNCTION_NAME
    assert found[TRIGGER_TRUNCATE][0] == FUNCTION_NAME
    assert found[TRIGGER_ROW][1] == "O", "триггер должен быть enabled (O)"
    assert found[TRIGGER_TRUNCATE][1] == "O", "триггер должен быть enabled (O)"


def test_update_raises():
    """UPDATE ledger_entries → RAISE, строка не меняется."""
    with _pg_conn() as conn:
        cur = conn.cursor()
        ledger_id = _seed_entry(cur)
        conn.commit()
        with pytest.raises(Exception) as exc:
            cur.execute(
                "UPDATE ledger_entries SET amount = amount WHERE ledger_id = %s",
                (ledger_id,),
            )
        conn.rollback()
    assert "append-only" in str(exc.value).lower()


def test_delete_raises():
    """DELETE ledger_entries → RAISE."""
    with _pg_conn() as conn:
        cur = conn.cursor()
        ledger_id = _seed_entry(cur)
        conn.commit()
        with pytest.raises(Exception) as exc:
            cur.execute("DELETE FROM ledger_entries WHERE ledger_id = %s", (ledger_id,))
        conn.rollback()
    assert "append-only" in str(exc.value).lower()


def test_truncate_raises():
    """TRUNCATE ledger_entries → RAISE (statement-level триггер)."""
    with _pg_conn() as conn:
        cur = conn.cursor()
        with pytest.raises(Exception) as exc:
            cur.execute("TRUNCATE TABLE ledger_entries")
        conn.rollback()
    assert "append-only" in str(exc.value).lower()


def test_insert_still_allowed():
    """INSERT не блокируется: append-only не означает read-only."""
    with _pg_conn() as conn:
        cur = conn.cursor()
        ledger_id = f"l1-insert-ok-{os.getpid()}"
        cur.execute(
            "INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)"
            " VALUES (%s, %s, 'CREDIT', 0.0, 'USD', '{}'::jsonb)"
            " ON CONFLICT (ledger_id) DO NOTHING",
            (ledger_id, "test-l1-trigger"),
        )
        conn.rollback()
