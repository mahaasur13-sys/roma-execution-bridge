"""
Enterprise Decision Gate — evaluates quota, cost, and policy checks
before allowing execution.  Week 1 implementation.
"""

from __future__ import annotations

import logging
import math
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
    def __init__(self, db_adapter=None):
        pass

    """Week 1: quota check + minimal cost check."""

    def evaluate(self, tenant_id: str, payload: dict | None = None) -> GateDecision:
        tenant = db.get_tenant(tenant_id)
        if not tenant:
            return GateDecision(GateResult.DENIED, "tenant not found", tenant_id)

        plan = tenant.get("plan", "free")
        plans = db._load_plans()
        plan_cfg = plans.get(plan, plans.get("free", {"max_jobs": 10}))
        max_jobs = plan_cfg.get("max_jobs", 10)

        # Quota is cumulative (plan = N jobs per period), not concurrency:
        # `max_concurrent` is a separate, per-plan knob enforced elsewhere.
        job_count = db.count_jobs_for_tenant_total(tenant_id)

        # Negative limit means "unlimited" (enterprise plans).
        if max_jobs >= 0 and job_count >= max_jobs:
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


def estimate_cluster_cost(cluster_spec: dict) -> float:
    """Cluster admission cost estimate (RaaS-04).

    ``nodes`` and ``expected_gpu_seconds`` are REQUIRED and must be >= 1; a
    missing/invalid value raises ValueError so the caller refuses the job instead
    of silently substituting a default. The per-second rate (0.00001) is the same
    one used by billing/finalize.py (``gpu_rate``).
    """
    nodes = cluster_spec.get("nodes")
    expected_gpu_seconds = cluster_spec.get("expected_gpu_seconds")
    if nodes is None or expected_gpu_seconds is None:
        raise ValueError(
            "cluster_spec requires 'nodes' and 'expected_gpu_seconds' (both >= 1)"
        )
    nodes = float(nodes)
    expected_gpu_seconds = float(expected_gpu_seconds)
    if not math.isfinite(nodes) or not math.isfinite(expected_gpu_seconds):
        raise ValueError(
            "'nodes' and 'expected_gpu_seconds' must be finite (not NaN/inf)"
        )
    if nodes < 1 or expected_gpu_seconds < 1:
        raise ValueError("'nodes' and 'expected_gpu_seconds' must be >= 1")
    return round(nodes * expected_gpu_seconds * 0.00001, 8)
