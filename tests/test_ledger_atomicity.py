"""Тесты для L4 debit_if_funds (атомарный дебит) — образец для ROMA.

Копировать в ~/roma-execution-bridge/tests/ ПОСЛЕ наката (не сейчас).
Запуск:
  cd ~/roma-execution-bridge
  .venv/bin/python -m pytest tests/test_ledger_atomicity.py -q

Первые 4 теста — юнит (мок _pg_execute, без PG).
Последний — проверка триггера append-only (нужен PG_DSN; иначе skip, мутаций нет — ROLLBACK).
"""
import pytest

from billing.pg_ledger import PGBillingLedger
from billing.pg_connection import PGUnavailableError


@pytest.fixture
def ledger():
    return PGBillingLedger()


def test_insufficient_funds_returns_none(ledger, monkeypatch):
    """Недостаточно средств (CTE вставил 0 строк) → None, fallback не пишем."""
    monkeypatch.setattr(ledger, "_pg_execute", lambda *a, **k: [])
    assert ledger.debit_if_funds("t-test", 1.0) is None
    # fail-closed: in-memory fallback НЕ должен пополниться
    assert ledger._entries == []


def test_success_micro_debit_returns_ledger_id(ledger, monkeypatch):
    """Успешный micro-debit → возвращается ledger_id."""
    monkeypatch.setattr(ledger, "_pg_execute", lambda *a, **k: [("led-1",)])
    assert ledger.debit_if_funds("t-test", 0.0000667) == "led-1"


def test_pg_down_fails_closed(ledger, monkeypatch):
    """PG недоступен → None (минус не пишется)."""

    def boom(*a, **k):
        raise PGUnavailableError("pg down")

    monkeypatch.setattr(ledger, "_pg_execute", boom)
    assert ledger.debit_if_funds("t-test", 1.0) is None


def test_sql_has_balance_guard_and_same_columns(ledger, monkeypatch):
    """Запрос: те же колонки INSERT + условная вставка WHERE balance >= amount."""
    captured = {}

    def fake(operation, query, params=None, fetch=False):
        captured["operation"] = operation
        captured["query"] = query
        captured["params"] = params
        return [("led-1",)]

    monkeypatch.setattr(ledger, "_pg_execute", fake)
    ledger.debit_if_funds("t-test", 0.5)

    assert captured["operation"] == "ledger_debit_if_funds"
    assert "INSERT INTO ledger_entries (ledger_id, tenant_id, entry_type, amount, currency, metadata)" in captured["query"]
    assert "WHERE bal.b >= %s" in captured["query"]
    # последний параметр = порог amount
    assert captured["params"][-1] == 0.5


def test_ledger_append_only_trigger():
    """UPDATE/DELETE по ledger_entries должны падать (триггер L3). Нужен PG_DSN."""
    import os
    if not os.environ.get("PG_DSN"):
        pytest.skip("PG_DSN не задан — триггер проверяется вручную (psql)")

    from billing.pg_connection import get_pg_manager
    mgr = get_pg_manager()
    conn = mgr.get_connection("test_append_only")
    try:
        cur = conn.cursor()
        cur.execute("SELECT ledger_id FROM ledger_entries LIMIT 1")
        row = cur.fetchone()
        if row is None:
            pytest.skip("ledger_entries пуста — нет строки для триггера")
        lid = row[0]
        cur.execute("BEGIN")
        try:
            with pytest.raises(Exception):
                cur.execute("UPDATE ledger_entries SET amount = amount WHERE ledger_id = %s", (lid,))
        finally:
            cur.execute("ROLLBACK")
    finally:
        conn.close()
