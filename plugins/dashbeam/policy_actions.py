"""DashBeam Policy Actions for DecisionOS Policy Engine.

Registers three new policy actions:
  dashbeam:ticket:create    — All tenants (FREE+)
  dashbeam:file:send        — All tenants, max file size by tier
  dashbeam:relay:configure  — Enterprise only
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class DashBeamPolicyAction(StrEnum):
    TICKET_CREATE = "dashbeam:ticket:create"
    FILE_SEND = "dashbeam:file:send"
    RELAY_CONFIGURE = "dashbeam:relay:configure"


# Per-tier file size limits (bytes)
TIER_FILE_SIZE_LIMITS: dict[str, int] = {
    "free": 100 * 1024 * 1024,        # 100 MB
    "pro": 500 * 1024 * 1024,         # 500 MB
    "enterprise": 10 * 1024 * 1024 * 1024,  # 10 GB
}

# Per-tier max tickets per day
TIER_TICKET_LIMITS: dict[str, int] = {
    "free": 10,
    "pro": 100,
    "enterprise": 10_000,
}


def can_create_ticket(tenant_id: str, tier: str, daily_count: int) -> tuple[bool, str]:
    """Policy check: can this tenant create a DashBeam ticket?"""
    if tier not in TIER_TICKET_LIMITS:
        return False, f"Unknown tier: {tier}"
    if daily_count >= TIER_TICKET_LIMITS[tier]:
        return False, f"Daily ticket limit ({TIER_TICKET_LIMITS[tier]}) exceeded"
    return True, "allowed"


def can_send_file(tenant_id: str, tier: str, file_size: int) -> tuple[bool, str]:
    """Policy check: is this file within the tier's size limit?"""
    limit = TIER_FILE_SIZE_LIMITS.get(tier, 0)
    if file_size > limit:
        return False, f"File size {file_size} exceeds tier limit {limit}"
    return True, "allowed"


def can_configure_relay(tenant_id: str, tier: str, is_custom: bool) -> tuple[bool, str]:
    """Policy check: can this tenant configure a custom relay?"""
    if is_custom and tier != "enterprise":
        return False, "Custom relay requires Enterprise tier"
    return True, "allowed"


def build_policy_action_context(
    action: DashBeamPolicyAction,
    tenant_id: str,
    tier: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build the context dict for a policy engine decision."""
    return {
        "action": action.value,
        "tenant_id": tenant_id,
        "tier": tier,
        **kwargs,
    }
