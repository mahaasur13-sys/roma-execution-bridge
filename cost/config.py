#!/usr/bin/env python3
"""Cost gate configuration — env-overridable, single source of truth."""

import os
from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class CostConfig:
    TIER_LIMITS: Dict[str, float] = field(
        default_factory=lambda: {
            "FREE": float(os.getenv("COST_LIMIT_FREE", "1.0")),
            "PRO": float(os.getenv("COST_LIMIT_PRO", "50.0")),
            "ENTERPRISE": float(os.getenv("COST_LIMIT_ENTERPRISE", "500.0")),
        }
    )

    DEFAULT_LIMIT: float = float(os.getenv("COST_LIMIT_DEFAULT", "10.0"))

    CONFIRMATION_THRESHOLD: float = float(
        os.getenv("COST_CONFIRMATION_THRESHOLD", "0.7")
    )

    MIN_CONFIDENCE_BASE: float = float(os.getenv("COST_MIN_CONFIDENCE_BASE", "0.75"))

    BENCHMARKS: Dict[str, int] = field(
        default_factory=lambda: {
            "ml_training": int(os.getenv("COST_BENCHMARK_ML_TRAINING", "7200")),
            "inference": int(os.getenv("COST_BENCHMARK_INFERENCE", "1800")),
            "simulation": int(os.getenv("COST_BENCHMARK_SIMULATION", "3600")),
            "data_processing": int(os.getenv("COST_BENCHMARK_DATA_PROCESSING", "5400")),
            "default": int(os.getenv("COST_BENCHMARK_DEFAULT", "3600")),
        }
    )

    GPU_FALLBACK_NODE: str = os.getenv("COST_GPU_FALLBACK_NODE", "gpu-node-1")

    CURRENCY: str = os.getenv("COST_CURRENCY", "USD")

    CURRENCY_SYMBOLS: Dict[str, str] = field(
        default_factory=lambda: {
            "USD": "$",
            "EUR": "€",
            "RUB": "₽",
            "GBP": "£",
        }
    )


_config: CostConfig | None = None


def get_cost_config() -> CostConfig:
    global _config
    if _config is None:
        _config = CostConfig()
    return _config
