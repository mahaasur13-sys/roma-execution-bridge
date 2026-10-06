#!/usr/bin/env python3
"""ROMA Cost Predictor — Pre-execution cost estimation engine.

G-PRICING-TIER-PATH (P3 PRICING-INTEGRITY): тариф — из записи клиента
(`db.get_tenant(tenant_id)["plan"]`), и только он. Прежнее поведение брало тариф из
payload (`job["tenant_tier"]`, дефолт "FREE") и через `tier_map.get(..., FREE)`
молча превращало платного клиента в free-тариф: цена считалась не по его плану.
Клиент без записи — отдельный отказ UNKNOWN_TENANT, а не silent-FREE.

G-FREE-DECISION-HEURISTIC (P3 PRICING-INTEGRITY / C3): вердикт FREE-тира решается
КВОТОЙ (джобы/мес и GPU-секунды из `config/plans.json`), а не деньгами. Прежнее
правило «FREE + длительность > 3600 → REQUIRES_CONFIRMATION» питалось флагом
FREE_TIER_LIMIT_RISK и обнулёнными free-ставками: решение о запуске строилось на
нуле. Флаг удалён целиком (недостижимая декорация после перехода на квоту), вердикт
обязан нести структурное основание (`decision_basis`). Недоступность счётчиков
потребления — отдельный fail-closed код GATE_UNAVAILABLE, а не тихий allow.
"""

import logging
import sys

sys.path.insert(0, '/home/workspace/roma-execution-bridge')
from billing.pricing_engine import PricingEngine, PricingTier
from monitoring import metrics as gate_metrics
import plan_source

logger = logging.getLogger("roma.cost.predictor")

FREE_PLAN = "free"

PLAN_TIER_MAP = {
    "free": PricingTier.FREE,
    "pro": PricingTier.PRO,
    "enterprise": PricingTier.ENTERPRISE,
}

UNKNOWN_TENANT = "UNKNOWN_TENANT"
UNKNOWN_PLAN = "UNKNOWN_PLAN"
NO_TENANT_REASON = (
    "tenant_id не задан: тариф не может быть установлен из записи клиента"
)
UNKNOWN_TENANT_REASON = (
    "клиент не найден в записях: тариф не установлен (не FREE по умолчанию)"
)


class UnknownPlanError(ValueError):
    """План клиента не отображён в тариф: молчаливый FREE запрещён (fail-closed)."""


def tenant_plan(tenant_id: str | None) -> str | None:
    """План клиента — единственный источник тарифа. None — клиента нет в записях."""
    if not tenant_id:
        return None
    import db_adapter as db

    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return None
    return tenant.get("plan")


def tier_from_plan(plan: str | None) -> PricingTier:
    """План записи клиента → тариф. План вне карты — отказ, а не FREE."""
    key = (plan or "").strip().lower()
    if key in PLAN_TIER_MAP:
        return PLAN_TIER_MAP[key]
    raise UnknownPlanError(f"план {plan!r} не отображён в тариф (маппинг только явный)")


class CostPredictor:
    """Predicts execution cost BEFORE running task."""

    def __init__(self):
        self.pricing = PricingEngine()
        self.tier_map = {
            "FREE": PricingTier.FREE,
            "PRO": PricingTier.PRO,
            "ENTERPRISE": PricingTier.ENTERPRISE,
        }

    def _tier_of_label(self, label: str) -> PricingTier:
        """Служебный (админский) канал: имя тарифа задано явно и подписано источником."""
        key = (label or "").strip().upper()
        if key in self.tier_map:
            return self.tier_map[key]
        raise UnknownPlanError(
            f"тариф {label!r} не объявлен — служебный канал тоже fail-closed"
        )

    def _resolve_tier(
        self, tenant_id: str | None, tenant_tier: str | None, admin_override: bool
    ) -> tuple[PricingTier | None, str | None, tuple[str, str] | None, str | None]:
        """Тариф из записи клиента; payload-тариф не авторитетен.

        Возвращает (tier, tier_source, refusal, plan_name): refusal — пара
        (код, причина); plan_name — имя тира в источнике квот (для вердикта по
        квоте нужен именно он, а не метка тарифа цены).
        """
        try:
            if admin_override:
                return (
                    self._tier_of_label(tenant_tier),
                    f"admin-override:{tenant_tier}",
                    None,
                    str(tenant_tier or "").strip().lower(),
                )
            if not tenant_id:
                return None, None, (UNKNOWN_TENANT, NO_TENANT_REASON), None
            plan = tenant_plan(tenant_id)
            if plan is None:
                return None, None, (UNKNOWN_TENANT, UNKNOWN_TENANT_REASON), None
            # Попытка подменить тариф payload-полем фиксируется в обе стороны
            # (и вверх, и вниз): тихой подмены нет, но и «FREE вместо FREE» —
            # не попытка, а дефолт вызывающего.
            payload_label = str(tenant_tier or "").strip().upper()
            if payload_label and payload_label != str(plan).strip().upper():
                logger.warning(
                    "cost.predictor: payload tenant_tier=%r игнорирован (tenant=%s) — "
                    "тариф берётся из записи клиента: %r",
                    tenant_tier,
                    tenant_id,
                    plan,
                )
            return (
                tier_from_plan(plan),
                f"tenant-record:{plan}",
                None,
                str(plan).strip().lower(),
            )
        except UnknownPlanError as exc:
            return None, None, (UNKNOWN_PLAN, str(exc)), None

    def predict(
        self,
        task: str,
        gpu_required: bool,
        plugin_type: str = "default",
        tenant_tier: str = "FREE",
        custom_duration: int = None,
        policy_engine=None,
        tenant_id: str | None = None,
        admin_override: bool = False,
    ) -> dict:
        tier_enum, tier_source, refusal, plan_name = self._resolve_tier(
            tenant_id, tenant_tier, admin_override
        )

        # Оцениваем длительность (в секундах)
        if custom_duration:
            duration_sec = custom_duration
        else:
            duration_sec = self._estimate_runtime(task, plugin_type, gpu_required)

        gpu_seconds = duration_sec if gpu_required else 0
        cpu_seconds = duration_sec if not gpu_required else 0
        storage_sec = 0  # storage пока не учитываем, но можно передать 0

        # G-PRICING-TIER-PATH: и оценка риска, и вердикт строятся на РАЗРЕШЁННОМ тарифе
        # (из записи клиента), а не на payload-поле: иначе платный клиент получал бы
        # free-флаги риска и free-вердикт, которые к его плану не относятся.
        plan_label = tier_enum.name if tier_enum is not None else None
        risk_flags = self._assess_risk(duration_sec, plan_label, task)

        gpu_node = "cpu-cluster"
        gpu_count = 0

        if refusal is not None:
            code, reason = refusal
            logger.warning(
                "cost.predictor: ценообразование отказано — %s (%s) tenant=%r",
                code,
                reason,
                tenant_id,
            )
            return {
                "estimated_cost": None,
                "estimated_duration_minutes": round(duration_sec / 60, 1),
                "gpu_node": "unpriced",
                "gpu_count": 0,
                "risk_level": "UNKNOWN",
                "currency": "USD",
                "confidence": self._confidence_score(plugin_type, duration_sec),
                "tier": None,
                "tier_source": None,
                "breakdown": {
                    "duration_sec": duration_sec,
                    "gpu_seconds": gpu_seconds,
                    "cpu_seconds": cpu_seconds,
                    "gpu_cost": None,
                    "cpu_cost": None,
                    "storage_cost": None,
                    "tier": None,
                    "tier_source": None,
                    "multiplier": self.pricing.multiplier,
                    "total": None,
                    "plugin_type": plugin_type,
                    "gpu_required": gpu_required,
                    "tenant_id": tenant_id,
                },
                "risk_flags": risk_flags,
                "decision": code,
                "decision_reason": reason,
            }

        cost = self.pricing.calculate(
            tier_enum, gpu_s=gpu_seconds, cpu_s=cpu_seconds, gb_s=storage_sec
        )
        total_cost = cost["total"]

        # G-FREE-DECISION-HEURISTIC: FREE решается квотой (джобы/мес и GPU-секунды
        # источника), платные тиры — прежним денежным правилом (неизменно).
        verdict = (
            self._quota_verdict(plan_name, gpu_seconds, tenant_id)
            if plan_name == FREE_PLAN
            else self._money_verdict(total_cost, plan_label)
        )

        # Уровень риска для CLI
        if risk_flags:
            risk_level = "MEDIUM" if "HIGH_COMPUTE_TASK" in risk_flags else "LOW"
        else:
            risk_level = "LOW"

        # Resolve GPU node from policy engine topology, or fallback
        if gpu_required:
            if policy_engine is not None:
                try:
                    best = policy_engine.select_best_node(vram_gb=8)
                    if best:
                        gpu_node = best.name
                        gpu_count = 1
                    else:
                        gpu_node = "gpu-node-1"
                        gpu_count = 1
                except Exception:
                    gpu_node = "gpu-node-1"
                    gpu_count = 1
            else:
                gpu_node = "gpu-node-1"
                gpu_count = 1

        return {
            "estimated_cost": round(total_cost, 4),
            "estimated_duration_minutes": round(duration_sec / 60, 1),
            "gpu_node": gpu_node,
            "gpu_count": gpu_count,
            "risk_level": risk_level,
            "currency": "USD",
            "confidence": self._confidence_score(plugin_type, duration_sec),
            "tier": tier_enum.value,
            "tier_source": tier_source,
            "breakdown": {
                "duration_sec": duration_sec,
                "gpu_seconds": gpu_seconds,
                "cpu_seconds": cpu_seconds,
                "gpu_cost": cost["gpu_cost"],
                "cpu_cost": cost["cpu_cost"],
                "storage_cost": cost["storage_cost"],
                "tier": tier_enum.value,
                "tier_source": tier_source,
                "multiplier": self.pricing.multiplier,
                "total": round(total_cost, 4),
                "plugin_type": plugin_type,
                "gpu_required": gpu_required,
                "tenant_id": tenant_id,
            },
            "risk_flags": risk_flags,
            "decision": verdict["decision"],
            "decision_reason": verdict["reason"],
            "decision_category": verdict["category"],
            "decision_basis": verdict["basis"],
            "decision_distance": verdict.get("distance"),
        }

    def _estimate_runtime(self, task: str, plugin_type: str, gpu_required: bool) -> int:
        benchmarks = {
            "ml_training": 7200,
            "inference": 1800,
            "simulation": 3600,
            "data_processing": 5400,
            "default": 3600,
        }
        base = benchmarks.get(plugin_type, 3600)
        if gpu_required:
            base = int(base * 1.1)
        return base

    def _assess_risk(self, duration_sec: int, tier: str, task: str) -> list:
        """Флаги риска — только про саму задачу.

        G-FREE-DECISION-HEURISTIC: `FREE_TIER_LIMIT_RISK` удалён целиком. После
        перехода вердикта FREE на квоту каждый его сценарий (FREE + duration > 3600)
        и есть превышение per-job сметы (`gpu_s_per_job` = 3600) — то есть
        REJECTED/QUOTA_JOB_ESTIMATE_EXCEEDS_PER_JOB с расстоянием до предела, а не
        флаг-декорация. Недостижимый код = мёртвый код.
        """
        flags = []
        if duration_sec > 36000:
            flags.append("LONG_RUNNING_TASK")
        if "yolov8" in task.lower() or "llm" in task.lower():
            flags.append("HIGH_COMPUTE_TASK")
        return flags

    def _confidence_score(self, plugin_type: str, duration_sec: int) -> float:
        base = 0.75
        if plugin_type in {"ml_training", "inference", "simulation"}:
            base += 0.15
        if 1800 <= duration_sec <= 14400:
            base += 0.10
        return min(base, 0.98)

    def _quota_verdict(
        self, plan_name: str, estimate_gpu_s: int, tenant_id: str | None
    ) -> dict:
        """Вердикт FREE-тира — двумерный предикат по квоте, с основанием.

        Счётчики потребления берутся только из леджера usage (обе ветки леджера —
        через `db.get_tenant_usage_db`). Недоступен леджер или источник квот →
        GATE_UNAVAILABLE (fail-closed), блокировка у потребителей; молчаливого
        allow нет — именно он и был классом G-GATE-FAILOPEN.
        """
        try:
            limits = plan_source.plan_limits(plan_name)
            consumption = plan_source.ledger_consumption(tenant_id)
        except plan_source.UsageLedgerUnavailable as exc:
            return self._gate_unavailable(
                "predictor.ledger",
                plan_name,
                tenant_id,
                f"счётчики потребления недоступны: {exc}",
            )
        except plan_source.PlanSourceError as exc:
            return self._gate_unavailable(
                "predictor.plan_source",
                plan_name,
                tenant_id,
                f"источник квот недоступен: {exc}",
            )
        return plan_source.free_quota_verdict(limits, consumption, estimate_gpu_s)

    def _gate_unavailable(
        self, scope: str, plan_name: str, tenant_id: str | None, reason: str
    ) -> dict:
        """Fail-closed отказ гейта: отдельный код, громкий лог, метрика наружу."""
        gate_metrics.track_gate_unavailable(scope)
        logger.error(
            "GATE_UNAVAILABLE (%s): %s · tenant=%r plan=%r — исполнение блокируется",
            scope,
            reason,
            tenant_id,
            plan_name,
        )
        return {
            "decision": plan_source.GATE_UNAVAILABLE,
            "category": plan_source.GATE_UNAVAILABLE,
            "reason": reason,
            "basis": {"tier": plan_name, "tenant_id": tenant_id, "scope": scope},
        }

    def _money_verdict(self, cost: float, tier: str) -> dict:
        """Денежный вердикт платных тиров — поведение неизменно.

        Ветка `risk_flags and tier == "FREE"` удалена: это и был механизм
        автоподтверждения на обнулённом free-нуле (правило «cost == 0 →
        подтверждение»), а его питающий флаг удалён целиком.
        """
        limits = {"FREE": 1.0, "PRO": 50.0, "ENTERPRISE": 500.0}
        limit = limits.get(tier, 10.0)
        basis = {"tier": tier, "cost_usd": cost, "tier_limit_usd": limit}
        if cost > limit or cost > limit * 0.8:
            return {
                "decision": "REQUIRES_CONFIRMATION",
                "category": "COST_ABOVE_TIER_LIMIT",
                "reason": f"стоимость ${cost:.2f} против денежного лимита тира ${limit:.2f}",
                "basis": {**basis, "proximity_pct": round(cost / limit * 100, 2)},
            }
        return {
            "decision": "APPROVED",
            "category": None,
            "reason": f"стоимость ${cost:.2f} в пределах денежного лимита тира ${limit:.2f}",
            "basis": {**basis, "proximity_pct": round(cost / limit * 100, 2)},
        }
