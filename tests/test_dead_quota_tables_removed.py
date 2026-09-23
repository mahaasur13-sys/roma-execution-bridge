"""G-QUOTA-SOURCE-REMNANTS (P3.6, коммит C2): мёртвые таблицы квот убраны вместе с читателями.

Класс дефекта (не инстанс): тарифные лимиты хранились не только в подписанном
`config/plans.json`, но и в модулях, которые никто не читал — `billing/cloudpayments_client.PLANS`
(free 50/0h · pro 500/20h · ent 999999/200h) и `cost/config.py` (TIER_LIMITS/BENCHMARKS).
Мёртвая таблица — это не «безвредный дубликат»: она описывает продукт иначе, чем
источник, и в любой момент может быть подключена «по аналогии», вернув расхождение
(ровно то, что случилось с `deps.PLANS`).

Проверки — тривиальность импортов после удаления: ни один живой потребитель не
сломался, а удалённые сущности не вернулись.
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest


def _spec_exists(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except ModuleNotFoundError:
        return False


def test_cloudpayments_price_table_is_gone() -> None:
    """`PLANS` из cloudpayments-клиента удалён: модуль импортируется, таблицы нет."""
    module = importlib.import_module("billing.cloudpayments_client")
    assert not hasattr(
        module, "PLANS"
    ), "мёртвая таблица квот вернулась в cloudpayments-клиент"


def test_cloudpayments_public_surface_intact() -> None:
    """Живая поверхность клиента (конфиг + клиент) на месте — удалялась только таблица."""
    module = importlib.import_module("billing.cloudpayments_client")
    assert hasattr(module, "CloudPaymentsConfig")
    assert hasattr(module, "CloudPaymentsClient")
    cfg = module.CloudPaymentsConfig(
        public_id="pk", api_secret="sk", webhook_secret="wh"
    )
    assert cfg.public_id == "pk"


def test_cost_config_module_is_gone() -> None:
    """`cost/config.py` (деньги-лимиты + бенчмарки, 0 читателей) удалён целиком."""
    assert not _spec_exists("cost.config"), "мёртвый модуль cost/config.py вернулся"


def test_live_consumers_still_import() -> None:
    """Живые потребители квот/денег импортируются после удаления мёртвых таблиц."""
    for dotted in (
        "main",
        "routers.billing",
        "cost.gate",
        "cost.predictor",
        "plan_source",
    ):
        assert importlib.import_module(dotted) is not None


def test_cost_package_keeps_its_live_modules() -> None:
    """Пакет `cost` не осиротел: живые модули гейта/оценки на месте."""
    for dotted in (
        "cost.gate",
        "cost.predictor",
        "cost.estimator",
        "cost.enterprise_gate",
    ):
        assert _spec_exists(
            dotted
        ), f"живой модуль {dotted} потерян при удалении мёртвых таблиц"
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("cost.config")
