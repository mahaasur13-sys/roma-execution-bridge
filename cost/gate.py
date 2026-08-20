"""
Enterprise Decision Gate — evaluates quota, cost, and policy checks
before allowing execution.  Week 1 implementation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import db_adapter as db

logger = logging.getLogger("roma.gate")


class GateResult(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass
class GateDecision:
    result: GateResult
    reason: str
    tenant_id: str
    job_limit: int = 0
    cost_estimated: float = 0.0


class EnterpriseDecisionGate:
    """Week 1: quota check + minimal cost check."""

    def evaluate(self, tenant_id: str, payload: dict | None = None) -> GateDecision:
        tenant = db.get_tenant(tenant_id)
        if not tenant:
            return GateDecision(GateResult.DENIED, "tenant not found", tenant_id)

        plan = tenant.get("plan", "free")
        plan_cfg = db._load_plans().get(plan, db._load_plans().get("free", {"max_jobs": 10}))
        max_jobs = plan_cfg.get("max_jobs", 10)

        job_count = db.count_jobs_for_tenant(tenant_id)

        if job_count >= max_jobs:
            return GateDecision(
                GateResult.DENIED,
                f"quota exceeded: {job_count}/{max_jobs} jobs",
                tenant_id,
                job_limit=max_jobs,
            )

        cost_est = estimate_cost(payload or {})
        return GateDecision(
            GateResult.ALLOWED,
            "quota ok",
            tenant_id,
            job_limit=max_jobs,
            cost_estimated=cost_est,
        )


# Fallback module-level function for main.py compatibility
def evaluate(tenant_id: str, payload: dict | None = None) -> GateDecision:
    gate = EnterpriseDecisionGate()
    return gate.evaluate(tenant_id, payload)


def estimate_cost(payload: dict) -> float:
    gpu_req = payload.get("gpu_required", False)
    base_cost = 0.002 if gpu_req else 0.0001
    return round(base_cost, 5)
