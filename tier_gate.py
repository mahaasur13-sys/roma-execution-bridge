"""DecisionOS — Tier-aware Gate (Week 4). Extends cost/gate.py with tier limits."""

import db_adapter as db
from tier_profiles import get_tier, DEFAULT_TIERS


def get_tier_limits(tenant_id: str) -> dict:
    """Get effective limits for a tenant based on their tier."""
    tenant = db.get_tenant(tenant_id)
    tier_name = tenant.get("tier", "start") if tenant else "start"
    tier = get_tier(tier_name) or DEFAULT_TIERS.get("start", {})
    return {
        "max_jobs": tier.get("max_jobs_month", 50),
        "max_gpu_seconds": tier.get("max_gpu_seconds_month", 36000),
        "max_concurrent": tier.get("max_concurrent", 5),
        "budget_limit": tier.get("budget_limit", 100.0),
        "audit_retention_days": tier.get("audit_retention_days", 7),
        "policy_engine_mode": tier.get("policy_engine_mode", "basic"),
        "ai_tools_mode": tier.get("ai_tools_mode", "read_only"),
    }
