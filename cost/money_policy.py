"""MONEY_WHITELIST_POLICY — именованный источник истины денежного скоупа.

Перенесено из `ci/coverage_gate.sh` (шапка :1-29, money-часть) — смысл-в-смысл,
не стиранием. Это ЕДИНСТВЕННОЕ место, где описано, какие файлы/таблицы/колонки
входят в MONEY-скоуп и каков их договор. Дрейф между этим модулем, конфигом
ratchet'а (`.ci/coverage-thresholds.json -> money_scope`), скриптом
(`ci/coverage_gate.sh` дефолты) и схемой БД — красный fail-closed
(см. `tests/test_money_whitelist_policy.py::test_*`).

Правило вхождения/выхода из скоупа:
  - ФАЙЛ входит в money-скоуп, если его путь начинается с префикса из
    `MONEY_SCOPE["prefixes"]` ИЛИ имя содержит подстроку из
    `MONEY_SCOPE["name_contains"]`, и при этом НЕ начинается с
    `MONEY_SCOPE["exclude_prefixes"]`.
  - ТАБЛИЦА/КОЛОНКА входит в money-скоуп, если она перечислена в `MONEY_COLUMNS`.
  - Изменение скоупа — только правкой этого модуля + синхронной правкой
    конфига/скрипта/схемы; иначе consistency-тест красный.

G-MONEY-FLOAT-TYPES (находка P3.7): типы money-колонок зафиксированы фактом
(REAL/DOUBLE PRECISION); смена типов — запрет эпохи, канал — только миграции
(deploy-эпоха, рядом с G-AUDIT-MIGRATION-HARDENING).
"""

from __future__ import annotations

# Денежный ФАЙЛОВЫЙ скоуп ratchet'а (знаменатель MONEY PATH).
# Должен байт-в-байт совпадать с `.ci/coverage-thresholds.json -> money_scope`
# и с дефолтом `ci/coverage_gate.sh` (m_prefixes/m_contains/m_excl).
MONEY_SCOPE: dict[str, list[str]] = {
    "prefixes": ["billing/"],
    "name_contains": ["ledger", "idempotenc"],
    "exclude_prefixes": ["tests/"],
}

# Денежные ТАБЛИЦЫ/КОЛОНКИ (схема БД, факт). Поля контракта:
#   not_null      — колонка в DDL объявлена NOT NULL;
#   default       — DDL-значение по умолчанию (текст, как в миграции);
#   reject_none   — адаптер ОТКЛОНЯЕТ None до INSERT (fail-closed).
MONEY_COLUMNS: dict[str, dict[str, dict[str, object]]] = {
    "execution_jobs": {
        "cost_usd": {"not_null": True, "default": "0.0", "reject_none": False},
    },
    "ledger_entries": {
        "amount": {"not_null": True, "default": "0.0", "reject_none": True},
        "currency": {"not_null": True, "default": "'USD'", "reject_none": True},
    },
    "tenant_usage_totals": {
        "gpu_seconds": {"not_null": True, "default": "0.0", "reject_none": False},
        "cpu_seconds": {"not_null": True, "default": "0.0", "reject_none": False},
        "gb_seconds": {"not_null": True, "default": "0.0", "reject_none": False},
        "input_tokens": {"not_null": True, "default": "0", "reject_none": False},
        "output_tokens": {"not_null": True, "default": "0", "reject_none": False},
        "total_cost": {"not_null": True, "default": "0.0", "reject_none": False},
    },
}


def reject_nullable_money(table: str, column: str, value: object) -> None:
    """Fail-closed адаптер-валидация: None в reject_none-колонке — отказ до INSERT.

    Возвращает None (валидно) или поднимает ValueError с именем таблицы/колонки.
    Договор каждой колонки — из `MONEY_COLUMNS`; колонки с `reject_none=False`
    следуют семантике «None → DEFAULT» и здесь не проверяются.
    """
    spec = MONEY_COLUMNS.get(table, {}).get(column)
    if spec is None:
        return
    if spec["reject_none"] and value is None:
        raise ValueError(
            f"{table}.{column}: money-колонка NOT NULL по договору — None недопустим"
        )
