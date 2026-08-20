"""Tier Feature Toggle Plugin — enables/disables features based on tenant plan."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("roma.plugin.tier_toggle")

FEATURE_MATRIX: dict[str, set[str]] = {
    "free": {"basic_api", "community_support", "public_docs"},
    "pro": {"basic_api", "community_support", "public_docs",
            "full_api", "webhooks", "crypto_payments", "priority_support",
            "plugin_marketplace", "api_rate_boost", "extended_history"},
    "enterprise": {"basic_api", "community_support", "public_docs",
                   "full_api", "webhooks", "crypto_payments", "priority_support",
                   "plugin_marketplace", "api_rate_boost", "extended_history",
                   "custom_plugins", "private_monero", "dedicated_cluster",
                   "sla_99_9", "custom_billing", "audit_export", "sso"},
}


class TierFeatureTogglePlugin:
    """Controls feature visibility and access based on tenant tier."""

    name = "tier-feature-toggle"
    display_name = "Tier Feature Toggle"

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}

    def on_enable(self, config: dict[str, Any]) -> None:
        self._config = config

    async def run(self, action: str, **kwargs: Any) -> dict[str, Any]:
        if action == "get_features":
            return self._get_features(kwargs["tenant_id"], kwargs.get("plan", "free"))
        elif action == "check_feature":
            return self._check_feature(
                kwargs["tenant_id"], kwargs.get("plan", "free"), kwargs["feature"]
            )
        elif action == "compare_tiers":
            return self._compare_tiers()
        else:
            return {"error": f"Unknown action: {action}"}

    def _get_features(self, tenant_id: str, plan: str) -> dict[str, Any]:
        features = FEATURE_MATRIX.get(plan, FEATURE_MATRIX["free"])
        all_features = sorted(FEATURE_MATRIX["enterprise"])

        return {
            "tenant_id": tenant_id,
            "plan": plan,
            "enabled_features": sorted(features),
            "disabled_features": sorted(set(all_features) - features),
            "total_enabled": len(features),
            "total_available": len(all_features),
        }

    def _check_feature(self, tenant_id: str, plan: str, feature: str) -> dict[str, Any]:
        features = FEATURE_MATRIX.get(plan, FEATURE_MATRIX["free"])
        enabled = feature in features
        return {
            "tenant_id": tenant_id,
            "plan": plan,
            "feature": feature,
            "enabled": enabled,
            "reason": "Feature available in your tier" if enabled else f"Upgrade to unlock '{feature}'",
        }

    def _compare_tiers(self) -> dict[str, Any]:
        return {
            tier: {
                "feature_count": len(features),
                "features": sorted(features),
            }
            for tier, features in FEATURE_MATRIX.items()
        }
