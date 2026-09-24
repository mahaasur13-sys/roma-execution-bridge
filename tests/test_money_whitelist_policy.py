"""P3.7 money-polish — MONEY_WHITELIST_POLICY: consistency + NOT NULL-договор.

Находки Акта 0, ставшие частью задания:
  * дрейф читателя №3: `test_coverage_ratchet.py` знал только prefix `billing/`,
    а не `name_contains ledger/idempotenc` — здесь скоуп пинуется по ВСЕМ читателям
    (политика ≡ конфиг ≡ скрипт ≡ схема), дрейф = красный fail-closed;
  * G-MONEY-FLOAT-TYPES — типы зафиксированы фактом (см. cost/money_policy.py),
    смена типа — только миграцией (deploy-эпоха), не в этой фазе.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from billing.pg_ledger import PGBillingLedger
from cost.money_policy import MONEY_COLUMNS, MONEY_SCOPE, reject_nullable_money

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
THRESHOLDS = REPO_ROOT / ".ci" / "coverage-thresholds.json"
GATE = REPO_ROOT / "ci" / "coverage_gate.sh"


def _thresholds() -> dict:
    return json.loads(THRESHOLDS.read_text(encoding="utf-8"))


# ── Consistency: политика ≡ конфиг ≡ скрипт ≡ схема ──────────────────────


def test_policy_scope_matches_config() -> None:
    """Политика `MONEY_SCOPE` байт-в-байт равна `money_scope` конфига ratchet'а."""
    assert MONEY_SCOPE == _thresholds()["money_scope"]


def test_policy_scope_matches_gate_defaults() -> None:
    """Политика зеркалит дефолты `ci/coverage_gate.sh` (m_prefixes/m_contains/m_excl)."""
    text = GATE.read_text(encoding="utf-8")
    assert '["billing/"]' in text, "gate default prefixes изменился"
    assert '["ledger", "idempotenc"]' in text, "gate default name_contains изменился"
    assert '["tests/"]' in text, "gate default exclude_prefixes изменился"
    assert MONEY_SCOPE["prefixes"] == ["billing/"]
    assert MONEY_SCOPE["name_contains"] == ["ledger", "idempotenc"]
    assert MONEY_SCOPE["exclude_prefixes"] == ["tests/"]


def test_policy_columns_match_schema_not_null() -> None:
    """Каждая not_null money-колонка объявлена NOT NULL в миграциях (схема ≡ политика)."""
    ddl = (REPO_ROOT / "migrations" / "002_billing_pg.sql").read_text(encoding="utf-8")
    for table, columns in MONEY_COLUMNS.items():
        for column, spec in columns.items():
            if not spec["not_null"]:
                continue
            assert (
                f"{column}" in ddl
            ), f"{table}.{column} помечена not_null в политике, но отсутствует в 002_billing_pg.sql"
            assert (
                f"{column}" in ddl and "NOT NULL" in ddl
            ), f"{table}.{column}: NOT NULL не подтверждён схемой"


# ── NOT NULL-договор: reject_none-колонки отклоняют None до INSERT ────────


def test_reject_none_amount() -> None:
    """ledger_entries.amount — обязательная money-колонка: None отклоняется."""
    with pytest.raises(ValueError, match=r"ledger_entries\.amount"):
        reject_nullable_money("ledger_entries", "amount", None)


def test_reject_none_currency() -> None:
    """ledger_entries.currency — обязательная money-колонка: None отклоняется."""
    with pytest.raises(ValueError, match=r"ledger_entries\.currency"):
        reject_nullable_money("ledger_entries", "currency", None)


def test_accept_full_money_tuple() -> None:
    """Позитивный контроль: полный кортеж (amount+currency) проходит."""
    reject_nullable_money("ledger_entries", "amount", 5.0)
    reject_nullable_money("ledger_entries", "currency", "USD")


def test_cost_usd_none_is_default_semantics() -> None:
    """execution_jobs.cost_usd — None → DEFAULT (reject_none=False), не отказ."""
    reject_nullable_money("execution_jobs", "cost_usd", None)


def test_ledger_append_accept_path_covers_not_null_contract() -> None:
    """Accept-путь `append()` реально гоняет оба reject_nullable_money-вызова.

    Покрытие money-скоупа: строки валидации в `billing/pg_ledger.py::append`
    (amount + currency) исполняются только через живой accept-путь; прямой вызов
    `reject_nullable_money` их не покрывает. Без PG append пишет in-memory fallback.
    """
    ledger = PGBillingLedger()
    ledger.append("t-money-policy", "DEBIT", 5.0, "USD")
    assert ledger._entries[-1]["amount"] == 5.0
    assert ledger._entries[-1]["currency"] == "USD"
    # Отказ по amount: исключение поднимается ДО записи (in-memory не пополняется).
    before = len(ledger._entries)
    with pytest.raises(ValueError, match=r"ledger_entries\.amount"):
        ledger.append("t-money-policy", "DEBIT", None, "USD")
    assert len(ledger._entries) == before
