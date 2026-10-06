#!/usr/bin/env python3
"""ROMA Multi-Tenant Manager — Per-tenant isolation, quotas, RBAC."""

import logging
import sys

sys.path.insert(0, '/home/workspace/roma-execution-bridge')

import plan_source

logger = logging.getLogger("roma.tenancy")


def _quota_gpu_seconds(plan: str) -> int:
    """Per-job GPU-квота тенанта — ПРОИЗВОДНОЕ источника `config/plans.json`.

    G-QUOTA-SOURCE-FRAGMENTED: здесь жила четвёртая таблица литералов
    (free 3600 / pro 144 000 / enterprise 999 999), не связанная с планами:
    «pro 144 000» не следовало ни из одного поля источника. Литералы удалены,
    значение читается из `gpu_s_per_job` (pro: 144 000 → 3600). Источник
    недоступен → 0 (отказ), а не выдуманная щедрая квота.
    """
    try:
        return plan_source.plan_limits(plan).gpu_s_per_job
    except plan_source.PlanSourceError as exc:
        logger.error(
            "GATE_UNAVAILABLE: квота тарифа %r не установлена (%s) — запрос отклоняется",
            plan,
            exc,
        )
        return 0


class TenantManager:
    """Manages tenant lifecycle, quotas, and RBAC."""

    def __init__(self):
        self.tenants = {
            "tenant-free": {
                "plan": "FREE",
                "quota_gpu_seconds": _quota_gpu_seconds("free"),
                "quota_monthly_usd": 10.0,
                "max_concurrent_jobs": 1,
                "gpu_available": True,
                "rbac_domain": "default",
                "features": ["ml_training", "inference"],
            },
            "tenant-pro": {
                "plan": "PRO",
                "quota_gpu_seconds": _quota_gpu_seconds("pro"),
                "quota_monthly_usd": 100.0,
                "max_concurrent_jobs": 5,
                "gpu_available": True,
                "rbac_domain": "pro",
                "features": [
                    "ml_training",
                    "inference",
                    "simulation",
                    "data_processing",
                ],
            },
            "tenant-enterprise": {
                "plan": "ENTERPRISE",
                "quota_gpu_seconds": _quota_gpu_seconds("enterprise"),
                "quota_monthly_usd": 9999.0,
                "max_concurrent_jobs": 50,
                "gpu_available": True,
                "rbac_domain": "enterprise",
                "features": [
                    "ml_training",
                    "inference",
                    "simulation",
                    "data_processing",
                    "custom",
                ],
            },
        }

    def get_tenant_info(self, tenant_id: str) -> dict:
        return self.tenants.get(tenant_id, self.tenants["tenant-free"])

    def enforce_quota(self, tenant_id: str, requested_gpu_seconds: float) -> dict:
        info = self.get_tenant_info(tenant_id)
        limit = info["quota_gpu_seconds"]
        # Отрицательное значение = безлимит (семантика источника), не «ноль остатка».
        if limit < 0:
            return {"allowed": True, "remaining": -1, "limit": limit}
        if requested_gpu_seconds > limit:
            return {
                "allowed": False,
                "reason": f"Requested {requested_gpu_seconds}s exceeds {info['plan']} limit of {limit}s",
                "upgrade_to": "PRO" if info["plan"] == "FREE" else "ENTERPRISE",
                "limit": limit,
            }
        return {"allowed": True, "remaining": limit - requested_gpu_seconds}


if __name__ == "__main__":
    tm = TenantManager()
    info = tm.get_tenant_info("tenant-pro")
    print(f"PRO tenant quota: {info['quota_gpu_seconds']} GPU-seconds/month")
    r = tm.enforce_quota("tenant-free", 7200)
    print(f"FREE tier 7200s request: allowed={r['allowed']}")
