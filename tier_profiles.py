"""DecisionOS — Pricing tiers: Start / Pro / Enterprise (Week 4)."""

import json
from pathlib import Path

TIERS_CONFIG_PATH = Path(__file__).parent / "tiers.json"

DEFAULT_TIERS = {
    "start": {
        "tier": "start",
        "max_jobs_month": 50,
        "max_gpu_seconds_month": 36000,
        "max_concurrent": 5,
        "budget_limit": 100.0,
        "audit_retention_days": 7,
        "white_label": False,
        "policy_engine_mode": "basic",
        "ai_tools_mode": "read_only",
        "sso": False,
    },
    "pro": {
        "tier": "pro",
        "max_jobs_month": 150,
        "max_gpu_seconds_month": 180000,
        "max_concurrent": 20,
        "budget_limit": 500.0,
        "audit_retention_days": 90,
        "white_label": False,
        "policy_engine_mode": "full",
        "ai_tools_mode": "scoped",
        "sso": False,
    },
    "enterprise": {
        "tier": "enterprise",
        "max_jobs_month": -1,
        "max_gpu_seconds_month": -1,
        "max_concurrent": 100,
        "budget_limit": -1,
        "audit_retention_days": -1,
        "white_label": True,
        "policy_engine_mode": "custom",
        "ai_tools_mode": "full",
        "sso": True,
    },
}


def load_tiers() -> dict:
    """Load tier profiles from config or return sensible defaults."""
    try:
        if TIERS_CONFIG_PATH.exists():
            return json.loads(TIERS_CONFIG_PATH.read_text())
    except Exception:
        pass
    return dict(DEFAULT_TIERS)


def get_tier(tier_name: str) -> dict | None:
    tiers = load_tiers()
    return tiers.get(tier_name)
