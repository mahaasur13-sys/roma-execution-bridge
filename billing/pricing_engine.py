#!/usr/bin/env python3
"""ROMA Pricing Engine — Dynamic pricing, tier management, cost models."""
from enum import Enum
from dataclasses import dataclass

class PricingTier(Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"

@dataclass
class TierConfig:
    name: str
    gpu_per_second: float
    cpu_per_second: float
    ram_per_second: float
    monthly_credit: float
    max_concurrent_jobs: int
    max_gpu_mb: int

TIERS = {
    PricingTier.FREE: TierConfig("Free", 0.0, 0.0, 0.0, 0.0, 2, 0),
    PricingTier.PRO:  TierConfig("Pro", 0.00001, 0.000001, 0.0000001, 50.0, 10, 8192),
    PricingTier.ENTERPRISE: TierConfig("Enterprise", 0.000008, 0.0000008, 0.00000008, 0.0, 100, 65536),
}

class PricingEngine:
    def __init__(self):
        self.utilization = 0.5

    def set_utilization(self, gpu_util: float):
        self.utilization = gpu_util

    def multiplier(self) -> float:
        if self.utilization > 0.9: return 2.0
        if self.utilization > 0.8: return 1.5
        if self.utilization > 0.7: return 1.2
        return 1.0

    def calculate(self, tier: PricingTier, gpu_s: float, cpu_s: float, gb_s: float) -> dict:
        cfg = TIERS[tier]
        base = gpu_s * cfg.gpu_per_second + cpu_s * cfg.cpu_per_second + gb_s * cfg.ram_per_second
        mult = self.multiplier()
        return {
            "tier": tier.value,
            "gpu_seconds": gpu_s, "cpu_seconds": cpu_s, "gb_seconds": gb_s,
            "base_cost": base,
            "multiplier": mult,
            "final_cost": round(base * mult, 6),
            "currency": "USD"
        }

if __name__ == "__main__":
    pe = PricingEngine()
    for util in [0.5, 0.75, 0.85, 0.95]:
        pe.set_utilization(util)
        r = pe.calculate(PricingTier.PRO, gpu_s=1000, cpu_s=3600, gb_s=86400)
        print(f"Util {util:.0%} → mult={r['multiplier']} → cost=${r['final_cost']:.4f}")
