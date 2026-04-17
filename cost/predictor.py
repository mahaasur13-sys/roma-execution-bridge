#!/usr/bin/env python3
"""ROMA Cost Predictor — Pre-execution cost estimation engine."""
import sys
sys.path.insert(0, '/home/workspace/roma-execution-bridge')

from billing.pricing_engine import PricingEngine, PricingTier, TIERS

class CostPredictor:
    """Predicts execution cost BEFORE running task."""

    def __init__(self):
        self.pricing = PricingEngine()
        self.tier_map = {"FREE": PricingTier.FREE, "PRO": PricingTier.PRO, "ENTERPRISE": PricingTier.ENTERPRISE}

    def predict(self, task: str, gpu_required: bool, plugin_type: str = "default",
                tenant_tier: str = "FREE", custom_duration: int = None) -> dict:
        tier_enum = self.tier_map.get(tenant_tier, PricingTier.FREE)

        if custom_duration:
            gpu_seconds = custom_duration * (1 if gpu_required else 0)
        else:
            gpu_seconds = self._estimate_runtime(task, plugin_type, gpu_required)

        result = self.pricing.calculate(tier_enum, 1, 0, 0)
        rate = result["base_cost"]
        util_multiplier = self.pricing.multiplier()
        effective_rate = rate * util_multiplier
        total_cost = gpu_seconds * effective_rate

        risk_flags = self._assess_risk(gpu_seconds, tenant_tier, task)

        return {
            "estimated_cost": round(total_cost, 4),
            "currency": "USD",
            "confidence": self._confidence_score(plugin_type, gpu_seconds),
            "breakdown": {
                "gpu_seconds": gpu_seconds,
                "rate_per_gpu_sec": round(rate, 6),
                "utilization_multiplier": round(util_multiplier, 2),
                "effective_rate": round(effective_rate, 6),
                "total": round(total_cost, 4),
                "plugin_type": plugin_type,
                "gpu_required": gpu_required
            },
            "risk_flags": risk_flags,
            "decision": self._decision(total_cost, tenant_tier, risk_flags)
        }

    def _estimate_runtime(self, task: str, plugin_type: str, gpu_required: bool) -> int:
        benchmarks = {"ml_training": 7200, "inference": 1800, "simulation": 3600, "data_processing": 5400, "default": 3600}
        base = benchmarks.get(plugin_type, 3600)
        if gpu_required:
            base = int(base * 1.1)
        return base

    def _assess_risk(self, gpu_seconds: int, tier: str, task: str) -> list:
        flags = []
        if gpu_seconds > 36000:
            flags.append("LONG_RUNNING_TASK")
        if tier == "FREE" and gpu_seconds > 3600:
            flags.append("FREE_TIER_LIMIT_RISK")
        if "yolov8" in task.lower() or "llm" in task.lower():
            flags.append("HIGH_COMPUTE_TASK")
        return flags

    def _confidence_score(self, plugin_type: str, gpu_seconds: int) -> float:
        base = 0.75
        if plugin_type in {"ml_training", "inference", "simulation"}:
            base += 0.15
        if 1800 <= gpu_seconds <= 14400:
            base += 0.10
        return min(base, 0.98)

    def _decision(self, cost: float, tier: str, risk_flags: list) -> str:
        limits = {"FREE": 1.0, "PRO": 50.0, "ENTERPRISE": 500.0}
        limit = limits.get(tier, 10.0)
        if cost > limit:
            return "REQUIRES_CONFIRMATION"
        if risk_flags and tier == "FREE":
            return "REQUIRES_CONFIRMATION"
        if cost > limit * 0.8:
            return "REQUIRES_CONFIRMATION"
        return "APPROVED"
