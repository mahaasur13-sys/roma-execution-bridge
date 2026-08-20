"""Custom Policy Plugin — rate limiting + geo-blocking example."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("roma.plugin.custom_policy")

# Simulate geo-blocking (no actual external calls)
BLOCKED_COUNTRIES = {"IR", "KP", "SY", "CU"}


class CustomPolicyPlugin:
    """Custom policy rules: rate limiting, geo-blocking, 2FA enforcement."""

    name = "custom-policy"
    display_name = "Custom Policy"

    def __init__(self) -> None:
        self._rate_limits: dict[str, list[float]] = {}
        self._config: dict[str, Any] = {}

    def on_enable(self, config: dict[str, Any]) -> None:
        self._config = config
        logger.info("CustomPolicyPlugin enabled: rate_limit=%s, blocked=%s",
                     config.get("rate_limit_per_minute"), config.get("blocked_countries"))

    def on_disable(self) -> None:
        self._rate_limits.clear()

    async def run(
        self,
        tenant_id: str,
        payload: dict[str, Any],
        tenant: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply custom policy rules."""
        results: list[dict[str, Any]] = []

        # Rule 1: geo-blocking
        geo_result = self._check_geo(payload.get("country", ""))
        if geo_result:
            results.append(geo_result)

        # Rule 2: rate limiting
        rate_result = self._check_rate_limit(tenant_id)
        if rate_result:
            results.append(rate_result)

        # Rule 3: 2FA enforcement for Pro+
        if self._config.get("require_2fa", False):
            if tenant.get("plan") in ("pro", "enterprise") and not tenant.get("2fa_enabled"):
                results.append({
                    "rule": "2fa_enforcement",
                    "action": "block",
                    "reason": "2FA required for Pro tier and above",
                })

        return {
            "plugin": self.name,
            "results": results,
            "overall": "deny" if any(r["action"] == "block" for r in results) else "allow",
        }

    def _check_geo(self, country: str) -> dict[str, Any] | None:
        if country.upper() in BLOCKED_COUNTRIES:
            return {
                "rule": "geo_blocking",
                "action": "block",
                "reason": f"Country {country} is blocked by geo-policy",
            }
        return None

    def _check_rate_limit(self, tenant_id: str) -> dict[str, Any] | None:
        import time
        now = time.time()
        window = self._rate_limits.setdefault(tenant_id, [])
        # Clean old entries
        window[:] = [t for t in window if now - t < 60]
        limit = self._config.get("rate_limit_per_minute", 60)
        if len(window) >= limit:
            return {
                "rule": "rate_limit",
                "action": "block",
                "reason": f"Rate limit exceeded ({limit}/minute)",
            }
        window.append(now)
        return None
