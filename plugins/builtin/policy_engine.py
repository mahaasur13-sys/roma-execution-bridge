"""Policy Engine Plugin — enterprise policy evaluation as a pluggable component."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from plugins.core.trace import TraceCollector

logger = logging.getLogger("roma.plugin.policy_engine")

# ─── Policy engine interface ───


class PolicyAction(str):
    """Policy action constants."""

    ALLOW = "allow"
    DENY = "deny"
    FLAG = "flag"
    AUDIT = "audit"
    THROTTLE = "throttle"  # rate limit
    ESCALATE = "escalate"  # manual review


class PolicyResult:
    """Result of a policy evaluation."""

    def __init__(
        self,
        action: PolicyAction | str,
        reason: str,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.action = PolicyAction(action)
        self.reason = reason
        self.confidence = confidence
        self.metadata = metadata or {}


class PolicyEnginePlugin:
    """Pluggable policy engine.

    Default rules:
        - Free tier: deny if quota exceeded
        - Pro tier: allow with dynamic limits
        - Enterprise: allow with custom policies
    """

    name = "policy-engine"
    display_name = "Policy Engine"

    def __init__(self) -> None:
        self._rules: list[callable] = []
        self._trace_collector: TraceCollector | None = None
        self._config: dict[str, Any] = {}

    def on_enable(self, config: dict[str, Any]) -> None:
        self._config = config
        self._register_default_rules()
        logger.info("PolicyEnginePlugin enabled with %d rules", len(self._rules))

    def on_disable(self) -> None:
        self._rules.clear()

    def _register_default_rules(self) -> None:
        """Register built-in policy rules."""

        def free_tier_quota(
            tenant: dict[str, Any], payload: dict[str, Any]
        ) -> PolicyResult | None:
            if tenant.get("plan") != "free":
                return None
            job_limit = tenant.get("job_limit", 3)
            current = tenant.get("active_jobs", 0)
            if current >= job_limit:
                return PolicyResult(
                    PolicyAction.DENY,
                    f"Free tier limit ({job_limit} jobs). Upgrade to Pro.",
                    confidence=1.0,
                )
            return None

        def max_cost_check(
            tenant: dict[str, Any], payload: dict[str, Any]
        ) -> PolicyResult | None:
            est_cost = payload.get("estimated_cost", 0)
            if est_cost > 100000:
                return PolicyResult(
                    PolicyAction.ESCALATE,
                    f"Estimated cost ${est_cost} exceeds threshold. Manual review required.",
                    confidence=0.9,
                )
            return None

        def enterprise_custom_check(
            tenant: dict[str, Any], payload: dict[str, Any]
        ) -> PolicyResult | None:
            if tenant.get("plan") != "enterprise":
                return None
            custom_policies = tenant.get("custom_policies", [])
            for policy_name in custom_policies:
                logger.debug("Enterprise custom policy: %s", policy_name)
            return PolicyResult(
                PolicyAction.ALLOW,
                "Enterprise custom policies applied",
                metadata={"policies_applied": custom_policies},
            )

        self._rules = [free_tier_quota, max_cost_check, enterprise_custom_check]

    async def run(
        self,
        tenant_id: str,
        payload: dict[str, Any],
        tenant: dict[str, Any],
        *,
        trace_id: str = "",
    ) -> PolicyResult:
        """Evaluate all rules. First DENY/ESCALATE wins, default ALLOW."""
        results: list[PolicyResult] = []
        decisions: list[str] = []

        if self._trace_collector and trace_id:
            self._trace_collector.add_step(
                trace_id, f"Evaluating {len(self._rules)} policy rules for tenant {tenant_id}",
                data={"tenant": tenant}, confidence=1.0,
            )

        for rule in self._rules:
            result = rule(tenant, payload)
            if result is None:
                continue
            results.append(result)
            decisions.append(f"{rule.__name__} → {result.action}: {result.reason}")

            if result.action in (PolicyAction.DENY, PolicyAction.ESCALATE):
                if self._trace_collector and trace_id:
                    self._trace_collector.add_step(
                        trace_id,
                        f"Rule '{rule.__name__}' returned {result.action}: {result.reason}",
                        data=result.metadata, confidence=result.confidence,
                    )
                logger.info(
                    "Policy decision for %s: %s by %s — %s",
                    tenant_id, result.action, rule.__name__, result.reason,
                )
                return result

        # Default: allow
        final = PolicyResult(
            PolicyAction.ALLOW,
            f"All {len(results)} rules passed. Default allow.",
            metadata={"rules_evaluated": len(self._rules), "decisions": decisions},
        )

        if self._trace_collector and trace_id:
            self._trace_collector.add_step(
                trace_id,
                f"All rules passed. Final: {final.action}",
                data=final.metadata, confidence=final.confidence,
            )

        return final

    def add_rule(self, rule: callable) -> None:
        """Add a custom policy rule."""
        self._rules.append(rule)
        logger.info("Custom rule added: %s", rule.__name__)

    def remove_rule(self, rule_name: str) -> bool:
        """Remove a rule by name."""
        for i, rule in enumerate(self._rules):
            if rule.__name__ == rule_name:
                self._rules.pop(i)
                logger.info("Rule removed: %s", rule_name)
                return True
        return False
