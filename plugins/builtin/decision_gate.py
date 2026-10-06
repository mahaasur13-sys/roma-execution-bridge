"""Decision Gate Plugin — cost gate and quota management as plugin."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger("roma.plugin.decision_gate")


class GateResult(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    FLAGGED = "flagged"


class GateDecision:
    """Decision from the gate."""

    def __init__(
        self,
        result: GateResult,
        reason: str,
        job_limit: int = 0,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.result = result
        self.reason = reason
        self.job_limit = job_limit
        self.confidence = confidence
        self.metadata = metadata or {}


class DecisionGatePlugin:
    """Pluggable Decision Gate.

    Evaluates cost, quota, and tier constraints.
    """

    name = "decision-gate"
    display_name = "Decision Gate"

    TIER_LIMITS: dict[str, dict[str, int | float]] = {
        "free": {"max_jobs": 3, "max_gpu_hours": 2.0, "max_concurrent": 1},
        "pro": {"max_jobs": 20, "max_gpu_hours": 500.0, "max_concurrent": 5},
        "enterprise": {"max_jobs": 500, "max_gpu_hours": 10000.0, "max_concurrent": 50},
    }

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}

    def on_enable(self, config: dict[str, Any]) -> None:
        self._config = config

    async def run(
        self,
        tenant_id: str,
        tenant: dict[str, Any],
        payload: dict[str, Any],
    ) -> GateDecision:
        """Evaluate if request should be allowed."""
        plan = tenant.get("plan", "free")
        limits = self.TIER_LIMITS.get(plan, self.TIER_LIMITS["free"])

        active_jobs = tenant.get("active_jobs", 0)
        max_jobs: int = int(limits["max_jobs"])

        if active_jobs >= max_jobs:
            return GateDecision(
                GateResult.DENIED,
                f"Job limit reached ({active_jobs}/{max_jobs}). Upgrade to Pro.",
                job_limit=max_jobs,
            )

        gpu_hours_used: float = float(tenant.get("gpu_hours_used", 0))
        max_gpu_hours: float = float(limits["max_gpu_hours"])
        estimated_hours: float = float(payload.get("gpu_hours", 1.0))

        if gpu_hours_used + estimated_hours > max_gpu_hours:
            return GateDecision(
                GateResult.DENIED,
                f"GPU hour quota would be exceeded (used: {gpu_hours_used}, needed: {estimated_hours})",
                job_limit=max_jobs,
            )

        return GateDecision(
            GateResult.ALLOWED,
            f"Quota OK ({active_jobs}/{max_jobs} jobs, {gpu_hours_used}/{max_gpu_hours} GPU hrs)",
            job_limit=max_jobs,
            confidence=0.95,
        )
