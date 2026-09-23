#!/usr/bin/env python3
"""G-QUOTA-SOURCE-FRAGMENTED — единственный источник квот.

Класс дефекта: тарифные лимиты хранились независимо в четырёх местах
(`config/plans.json`, `db_adapter._PLAN_DEFAULTS`, `auth/quota_engine.PLAN_QUOTAS`,
`tenancy/manager`), и каждая таблица дрейфовала сама по себе: на одну free-задачу
четыре источника отвечали по-разному (10 vs 50 джобов/мес, 300k vs 180k vs 3.6k
GPU-секунд). Здесь — единственная точка чтения и вывода.

Схема источника (`config/plans.json`), тир описывается ДВУМЯ полями:
  * `jobs_per_month` — джобов в месяц (−1 = безлимит);
  * `gpu_s_per_job`  — смета GPU-секунд на один джоб (−1 = безлимит).
Месячный ресурс — ПРОИЗВОДНОЕ (jobs × per_job) и отдельным литералом не хранится.

Счётчики потребления берутся только из леджера usage: одна нормализация
(`ledger_consumption`) на все потребители, обе ветки леджера (PG и SQLite) читаются
одной и той же таблицей источника — `usage_events` через `db.get_tenant_usage_db`.
Недоступность счётчиков — отдельный fail-closed код (`UsageLedgerUnavailable`
→ GATE_UNAVAILABLE у потребителей), молчаливого allow нет.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("roma.plan_source")

PLANS_PATH = Path(__file__).resolve().parent / "config" / "plans.json"

UNLIMITED = -1

# Коды отказов/вердиктов (единый словарь кодов, чтобы потребители не сравнивали строки вслепую)
GATE_UNAVAILABLE = "GATE_UNAVAILABLE"
APPROVED = "APPROVED"
REJECTED = "REJECTED"

QUOTA_JOBS_EXHAUSTED = "QUOTA_JOBS_EXHAUSTED"
QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB = "QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB"
QUOTA_MONTHLY_GPU_EXHAUSTED = "QUOTA_MONTHLY_GPU_EXHAUSTED"

UNKNOWN_TENANT = "UNKNOWN_TENANT"

# Объявленные разрешающие формы вердикта: всё вне этого множества — отказ
# (fail-closed хвост: неизвестное будущее не становится разрешением).
GATE_ALLOWED = "allowed"  # GateResult.ALLOWED.value (cost/gate.py)
REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
ALLOWED_VERDICTS = frozenset({APPROVED, REQUIRES_CONFIRMATION, GATE_ALLOWED})

# G-GATE-DENY-LOCAL-BYPASS: семейство отказов. Прежде отказ распознавался двумя
# кодами (UNKNOWN_TENANT, GATE_UNAVAILABLE), а вердикт REJECTED/QUOTA_* в маршруте
# не разбирался вовсе — отказ по квоте уходил в маршрут queued и исполнялся.
REJECTION_DECISIONS = frozenset(
    {
        UNKNOWN_TENANT,
        GATE_UNAVAILABLE,
        REJECTED,
        QUOTA_JOBS_EXHAUSTED,
        QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB,
        QUOTA_MONTHLY_GPU_EXHAUSTED,
    }
)


def is_rejection(decision: str | None, category: str | None = None) -> bool:
    """Единый предикат отказа по всему спектру решений, а не по двум знакомым кодам.

    Отказом считается: любой объявленный код семейства отказов (в поле решения или
    категории) и ЛЮБАЯ форма вне объявленных разрешающих — fail-closed хвост:
    неизвестное решение блокирует исполнение, а не разрешает его. Пустое
    решение — тоже отказ.
    """
    for code in (decision, category):
        if code and code in REJECTION_DECISIONS:
            return True
    return (decision or "") not in ALLOWED_VERDICTS


class PlanSourceError(RuntimeError):
    """Источник квот недоступен или тир вне объявленной схемы (fail-closed)."""


class UsageLedgerUnavailable(RuntimeError):
    """Счётчики потребления недоступны: вердикт по квоте вынести нельзя."""


@dataclass(frozen=True)
class PlanLimits:
    """Лимиты тира: два объявленных поля + производный месячный ресурс."""

    plan: str
    jobs_per_month: int
    gpu_s_per_job: int

    @property
    def gpu_s_per_month(self) -> int:
        """Производное: jobs_per_month × gpu_s_per_job (−1 = безлимит)."""
        if self.jobs_per_month < 0 or self.gpu_s_per_job < 0:
            return UNLIMITED
        return self.jobs_per_month * self.gpu_s_per_job

    @property
    def gpu_hours_per_month(self) -> int:
        """Производное представление месячного ресурса в GPU-часах (−1 = безлимит).

        Отдельным хранимым значением не существует: читатели, исторически
        спрашивавшие `max_gpu_hours`, получают вывод из двух объявленных полей.
        """
        monthly = self.gpu_s_per_month
        if monthly < 0:
            return UNLIMITED
        return monthly // 3600


@dataclass(frozen=True)
class Consumption:
    """Счётчики потребления из леджера usage."""

    tenant_id: str
    jobs_used: int
    gpu_seconds_used: int


def load_plans() -> dict:
    """Планы из `config/plans.json` — без выдуманных дефолтов (fail-closed)."""
    try:
        raw = json.loads(PLANS_PATH.read_text())
    except FileNotFoundError as exc:
        raise PlanSourceError(f"plans.json отсутствует: {PLANS_PATH}") from exc
    except (OSError, ValueError) as exc:
        raise PlanSourceError(
            f"plans.json нечитаем/невалиден ({PLANS_PATH}): {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not raw:
        raise PlanSourceError(f"plans.json пуст или не объект: {PLANS_PATH}")
    return raw


def plan_limits(plan: str | None) -> PlanLimits:
    """Лимиты тира из источника. Неизвестный/неполный тир — отказ, а не догадка."""
    key = (plan or "").strip().lower()
    if not key:
        raise PlanSourceError("имя тира пусто: лимиты не установлены")
    plans = load_plans()
    cfg = plans.get(key)
    if not isinstance(cfg, dict):
        raise PlanSourceError(f"тир {plan!r} отсутствует в источнике {PLANS_PATH}")
    missing = [f for f in ("jobs_per_month", "gpu_s_per_job") if f not in cfg]
    if missing:
        raise PlanSourceError(f"тир {key!r} неполон в источнике: нет полей {missing}")
    try:
        jobs_per_month = int(cfg["jobs_per_month"])
        gpu_s_per_job = int(cfg["gpu_s_per_job"])
    except (TypeError, ValueError) as exc:
        raise PlanSourceError(f"тир {key!r}: поля квот не целые числа ({exc})") from exc
    return PlanLimits(
        plan=key, jobs_per_month=jobs_per_month, gpu_s_per_job=gpu_s_per_job
    )


def ledger_consumption(tenant_id: str) -> Consumption:
    """Счётчики потребления из леджера usage (единственная точка нормализации).

    Обе ветки леджера (PG/SQLite) приходят сюда через `db.get_tenant_usage_db`,
    который читает одну и ту же таблицу источника — `usage_events`.
    """
    import db_adapter as db

    try:
        raw = db.get_tenant_usage_db(tenant_id)
    except Exception as exc:  # недоступность счётчиков — не молчаливый allow
        raise UsageLedgerUnavailable(
            f"леджер usage недоступен для {tenant_id!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise UsageLedgerUnavailable(
            f"леджер usage вернул неожиданный тип для {tenant_id!r}: {type(raw).__name__}"
        )
    try:
        return Consumption(
            tenant_id=tenant_id,
            jobs_used=int(raw.get("total_jobs", 0)),
            gpu_seconds_used=int(raw.get("total_gpu_seconds", 0)),
        )
    except (TypeError, ValueError) as exc:
        raise UsageLedgerUnavailable(
            f"леджер usage вернул неразбираемые счётчики для {tenant_id!r}: {exc}"
        ) from exc


def _unlimited(limit: int) -> bool:
    return limit < 0


def free_quota_verdict(
    limits: PlanLimits, consumption: Consumption, estimate_gpu_s: float
) -> dict:
    """Двумерный предикат по квоте (без денег и без эвристик длительности).

    Измерения: джобы за месяц и GPU-секунды (смета джоба против per-job лимита,
    потребление+смета против производного месячного лимита). Исчерпание любого
    измерения → REJECTED с категорией, причиной и расстоянием до предела.
    В пределах обоих → APPROVED со структурным основанием.
    """
    jobs_limit = limits.jobs_per_month
    per_job_cap = limits.gpu_s_per_job
    monthly_limit = limits.gpu_s_per_month
    estimate = int(estimate_gpu_s or 0)

    basis = {
        "tier": limits.plan,
        "jobs_used": consumption.jobs_used,
        "jobs_limit": jobs_limit,
        "estimate_gpu_s": estimate,
        "gpu_s_per_job": per_job_cap,
        "monthly_used_gpu_s": consumption.gpu_seconds_used,
        "monthly_limit_gpu_s": monthly_limit,
    }

    if not _unlimited(jobs_limit) and consumption.jobs_used >= jobs_limit:
        return {
            "decision": REJECTED,
            "category": QUOTA_JOBS_EXHAUSTED,
            "reason": (
                f"квота джобов исчерпана: {consumption.jobs_used}/{jobs_limit} "
                f"джобов за месяц (до предела: {jobs_limit - consumption.jobs_used})"
            ),
            "basis": {
                **basis,
                "distance": {"jobs_to_limit": jobs_limit - consumption.jobs_used},
                "proximity_pct": 100.0,
            },
        }

    if not _unlimited(per_job_cap) and estimate > per_job_cap:
        over = estimate - per_job_cap
        return {
            "decision": REJECTED,
            "category": QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB,
            "reason": (
                f"смета джоба превышает лимит тира: {estimate} > {per_job_cap} GPU-s "
                f"(превышение: {over})"
            ),
            "basis": {
                **basis,
                "distance": {"per_job_over_by_gpu_s": over},
                "proximity_pct": round(estimate / per_job_cap * 100, 2),
            },
        }

    projected = consumption.gpu_seconds_used + estimate
    if not _unlimited(monthly_limit) and projected > monthly_limit:
        over = projected - monthly_limit
        return {
            "decision": REJECTED,
            "category": QUOTA_MONTHLY_GPU_EXHAUSTED,
            "reason": (
                f"месячный ресурс GPU исчерпан: потребление {consumption.gpu_seconds_used} "
                f"+ смета {estimate} > {monthly_limit} GPU-s (превышение: {over})"
            ),
            "basis": {
                **basis,
                "distance": {"monthly_over_by_gpu_s": over},
                "projected_monthly_gpu_s": projected,
                "proximity_pct": round(projected / monthly_limit * 100, 2),
            },
        }

    ratios = []
    if not _unlimited(jobs_limit) and jobs_limit:
        ratios.append((consumption.jobs_used + 1) / jobs_limit)
    if not _unlimited(monthly_limit) and monthly_limit:
        ratios.append(projected / monthly_limit)
    proximity = round(max(ratios) * 100, 2) if ratios else 0.0

    return {
        "decision": APPROVED,
        "category": None,
        "reason": (
            f"квота в пределах тира {limits.plan}: джобы "
            f"{consumption.jobs_used}/{jobs_limit if not _unlimited(jobs_limit) else '∞'}, "
            f"смета {estimate}/{per_job_cap if not _unlimited(per_job_cap) else '∞'} GPU-s, "
            f"месяц {projected}/"
            f"{monthly_limit if not _unlimited(monthly_limit) else '∞'} GPU-s"
        ),
        "basis": {
            **basis,
            "distance": {
                "jobs_remaining": (
                    UNLIMITED
                    if _unlimited(jobs_limit)
                    else jobs_limit - consumption.jobs_used
                ),
                "monthly_gpu_s_remaining": (
                    UNLIMITED
                    if _unlimited(monthly_limit)
                    else monthly_limit - projected
                ),
            },
            "projected_monthly_gpu_s": projected,
            "proximity_pct": proximity,
        },
    }
