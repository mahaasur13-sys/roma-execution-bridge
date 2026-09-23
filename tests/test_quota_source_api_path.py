"""G-QUOTA-SOURCE-REMNANTS · C1: API submit-путь читает ту же квоту, что и источник.

Класс дефекта: квоты API-пути жили собственной литеральной таблицей (`deps.PLANS`),
дрейфовавшей от подписанного источника `config/plans.json`, и тариф `start` в
источнике отсутствовал вовсе. Живая дивергенция: pro-клиент получал 402 на 151-й
задаче через `POST /submit` (таблица: 150) и одобрение на той же задаче через
планировщик/CLI (источник: 1000) — «одна задача, два вердикта».

Здесь проверяются:
  1) границы по квоте для каждого объявленного тира — из источника, а не из литералов;
  2) `start` существует в источнике и несёт историческое значение (50 джобов/мес);
  3) тир вне объявленной схемы и клиент без записи — отказ `GATE_UNAVAILABLE`
     (fail-closed), а не молчаливый free-дефолт;
  4) `deps.PLANS` — вывод из источника (совместимость), а не вторая таблица.
"""

from __future__ import annotations

import inspect
import re

import pytest

import db_adapter as db
import main
import plan_source
from deps import PLANS, plan_config

GATE_UNAVAILABLE = plan_source.GATE_UNAVAILABLE


def _patch_tenant(monkeypatch, plan: str | None) -> None:
    """Запись клиента в единственном месте, откуда берётся тариф."""
    record = None if plan is None else {"tenant_id": "t-1", "plan": plan}
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: record)


def _patch_usage(monkeypatch, jobs_used: int) -> None:
    monkeypatch.setattr(db, "count_jobs_for_tenant_total", lambda tenant_id: jobs_used)


@pytest.mark.parametrize(
    ("plan", "jobs_used", "expected_allowed"),
    [
        ("free", 49, True),
        ("free", 50, False),
        ("start", 49, True),
        ("start", 50, False),
        ("pro", 150, True),
        ("pro", 999, True),
        ("pro", 1000, False),
        ("enterprise", 10**6, True),
    ],
)
def test_jobs_boundary_follows_source(
    monkeypatch, plan: str, jobs_used: int, expected_allowed: bool
) -> None:
    """Граница по джобам — ровно та, что объявлена в `config/plans.json`."""
    _patch_tenant(monkeypatch, plan)
    _patch_usage(monkeypatch, jobs_used)

    allowed, reason = main._check_limits("t-1")

    limit = plan_source.plan_limits(plan).jobs_per_month
    assert allowed is expected_allowed, (plan, jobs_used, reason)
    if expected_allowed:
        assert reason == ""
    elif limit < 0:
        raise AssertionError("безлимитный тир не может быть отклонён по квоте")
    else:
        assert reason == f"Monthly job limit reached: {jobs_used}/{limit}"


def test_pro_151st_job_is_accepted_once_and_denied_at_1001st(monkeypatch) -> None:
    """Якорь класса: 151-я задача pro-клиента прежде получала 402 (таблица: 150)."""
    _patch_tenant(monkeypatch, "pro")

    _patch_usage(monkeypatch, 150)
    assert main._check_limits("t-1") == (True, ""), "151-я задача обязана приниматься"

    _patch_usage(monkeypatch, 1000)
    allowed, reason = main._check_limits("t-1")
    assert allowed is False and reason == "Monthly job limit reached: 1000/1000"


def test_start_tier_carries_historical_value(monkeypatch) -> None:
    """`start` — первый класс продукта; в источнике его не было вовсе.

    Историческое наследие (pre-C3 `db_adapter._PLAN_DEFAULTS['start']`): 50 джобов
    в месяц. Значение подписано владельцем в чекпойнте P3.6 №1.
    """
    limits = plan_source.plan_limits("start")
    assert limits.jobs_per_month == 50
    assert limits.gpu_s_per_job == 3600

    _patch_tenant(monkeypatch, "start")
    _patch_usage(monkeypatch, 50)
    assert main._check_limits("t-1")[0] is False


def test_unknown_plan_is_gate_unavailable(monkeypatch) -> None:
    """Тир вне объявленной схемы — отказ, а не free-дефолт (fail-closed)."""
    _patch_tenant(monkeypatch, "platinum")
    _patch_usage(monkeypatch, 0)

    allowed, reason = main._check_limits("t-1")

    assert allowed is False
    assert reason.startswith(GATE_UNAVAILABLE)


def test_tenant_without_record_is_gate_unavailable(monkeypatch) -> None:
    """Клиент без записи не получает чужую квоту: тариф неизвестен → отказ."""
    _patch_tenant(monkeypatch, None)
    _patch_usage(monkeypatch, 0)

    allowed, reason = main._check_limits("t-1")

    assert allowed is False
    assert reason.startswith(GATE_UNAVAILABLE)


def test_deps_plans_is_derived_not_a_second_table() -> None:
    """`deps.PLANS` — вывод источника: каждая квота совпадает с `plan_source`."""
    for name in plan_source.load_plans():
        limits = plan_source.plan_limits(name)
        assert PLANS[name]["max_jobs_per_month"] == limits.jobs_per_month
        assert PLANS[name]["gpu_s_per_job"] == limits.gpu_s_per_job
        assert PLANS[name]["gpu_s_per_month"] == limits.gpu_s_per_month


def test_legacy_quota_key_is_gone() -> None:
    """Легаси-имя `max_gpu_seconds` (per-job и месячный смысл в одном поле) убрано."""
    for name in plan_source.load_plans():
        assert "max_gpu_seconds" not in PLANS[name]


def test_quota_literals_are_not_back_in_deps() -> None:
    """Статический страж: числовых литералов квот в `deps.py` быть не должно.

    Ключи в `plan_config` — вывод из `plan_source`; литералом является ЧИСЛО в
    таблице, а не имя поля. Ищем именно второе.
    """
    source = inspect.getsource(__import__("deps"))
    for pattern in (r'"max_jobs_per_month":\s*-?\d', r'"max_gpu_seconds":\s*-?\d'):
        assert not re.search(pattern, source), f"в deps.py вернулся литерал квоты по {pattern!r}"


def test_plan_config_carries_money_policy_separately() -> None:
    """Денежная политика — отдельная ось (G-MONEY-CAP-LITERALS), источник её не несёт."""
    cfg = plan_config("free")
    assert cfg["spend_cap_usd"] == 0.50 and cfg["overage_rate"] == 0.0
    assert "spend_cap_usd" not in plan_source.load_plans()["free"]

    with pytest.raises(plan_source.PlanSourceError):
        plan_config("platinum")
