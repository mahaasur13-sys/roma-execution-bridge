#!/usr/bin/env python3
"""ROMA Quota Engine — Per-tenant quota tracking and enforcement.

G-QUOTA-SOURCE-FRAGMENTED (P3 PRICING-INTEGRITY / C3): квоты больше не хранятся
литералами в этом модуле. Единственный источник — `config/plans.json` (через
`plan_source`), где тир описан двумя полями: jobs_per_month и gpu_s_per_job.
Месячный ресурс — производное (jobs × per_job), отдельным литералом не хранится.
Здесь остаётся только чтение-вывод: PLAN_QUOTAS строится из источника.
"""

from typing import Dict

from plan_source import plan_limits


def _derived_plan_quotas() -> Dict[str, int]:
    """GPU-секунды/месяц по тарифу — вывод из plans.json, не литерал."""
    quotas: Dict[str, int] = {}
    for plan in ("free", "pro", "enterprise"):
        quotas[plan.upper()] = plan_limits(plan).gpu_s_per_month
    return quotas


class QuotaEngine:
    """
    Tracks per-tenant quota usage against plan limits.
    Enforces GPU-second quotas per billing cycle.
    """

    PLAN_QUOTAS = _derived_plan_quotas()
    PLAN_PRIORITY = {
        "FREE": 1,
        "PRO": 2,
        "ENTERPRISE": 3,
    }

    def __init__(self, metering_engine=None):
        self.metering = metering_engine
        self.usage: Dict[str, float] = {}  # tenant_id → GPU-sec used
        self.cycle_start: Dict[str, float] = {}  # tenant_id → cycle start

    def get_usage(self, tenant_id: str) -> float:
        return self.usage.get(tenant_id, 0.0)

    def get_limit(self, tenant_id: str, plan: str = "FREE") -> float:
        return self.PLAN_QUOTAS.get(plan, 0.0)

    def check_quota(
        self, tenant_id: str, requested: float, plan: str = "FREE"
    ) -> tuple[bool, str]:
        used = self.get_usage(tenant_id)
        limit = self.get_limit(tenant_id, plan)
        # Отрицательное значение = безлимит (семантика источника), а не «0 остатка».
        if limit < 0:
            return True, f"Unlimited ({plan}): {used:.0f} GPU-s used"
        if used + requested > limit:
            return False, f"Quota exceeded: {used:.0f}/{limit:.0f} GPU-s"
        return True, "OK"

    def record_usage(self, tenant_id: str, gpu_seconds: float, plan: str = "FREE"):
        self.usage[tenant_id] = self.usage.get(tenant_id, 0.0) + gpu_seconds

    def estimate_cost(self, gpu_seconds: float, plan: str = "FREE") -> float:
        base_rate = 0.000222  # $ / GPU-second (from pricing_engine.py)
        plan_modifier = {"FREE": 1.0, "PRO": 0.9, "ENTERPRISE": 0.75}.get(plan, 1.0)
        return gpu_seconds * base_rate * plan_modifier

    def quota_headers(self, tenant_id: str, plan: str = "FREE") -> dict:
        used = self.get_usage(tenant_id)
        limit = self.get_limit(tenant_id, plan)
        remaining = -1 if limit < 0 else int(max(0, limit - used))
        return {
            "X-ROMA-Quota-Used": str(int(used)),
            "X-ROMA-Quota-Limit": str(int(limit)),
            "X-ROMA-Quota-Remaining": str(remaining),
        }
