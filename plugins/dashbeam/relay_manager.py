"""RelayManager — handles public and custom DashBeam relays.

Public relay: relay.dashbeam.io (default, all tiers)
Custom relay: Enterprise-only, user-specified URL + discovery nodes
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
import uuid

from plugins.dashbeam.domain import DashBeamRelayConfig


DEFAULT_RELAY = "https://relay.dashbeam.io"
DEFAULT_DISCOVERY = [
    "https://discovery1.dashbeam.io",
    "https://discovery2.dashbeam.io",
    "https://discovery3.dashbeam.io",
]


class RelayManager:
    """Manages relay configuration per tenant.

    Free/Pro tenants use the public DashBeam relay.
    Enterprise tenants can specify custom relay URLs and discovery nodes.
    """

    def __init__(self) -> None:
        self._configs: dict[str, DashBeamRelayConfig] = {}

    # ─── Public API ───

    def get_config(self, tenant_id: str) -> DashBeamRelayConfig:
        """Get relay config for a tenant. Falls back to default."""
        return self._configs.get(tenant_id, self._default_config(tenant_id))

    def set_config(
        self,
        tenant_id: str,
        relay_url: str,
        *,
        tier: str = "free",
        discovery_nodes: Optional[list[str]] = None,
        region: str = "auto",
        max_bandwidth_mbps: int = 100,
    ) -> DashBeamRelayConfig:
        """Set relay config. Custom relay only for Enterprise tier."""
        is_custom = relay_url != DEFAULT_RELAY
        if is_custom and tier != "enterprise":
            raise PermissionError(
                "Custom relay configuration requires Enterprise tier"
            )

        config = DashBeamRelayConfig(
            config_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            relay_url=relay_url,
            relay_region=region,
            is_custom=is_custom,
            max_bandwidth_mbps=max_bandwidth_mbps,
            discovery_nodes=discovery_nodes or DEFAULT_DISCOVERY,
            updated_at=datetime.now(timezone.utc),
        )
        self._configs[tenant_id] = config
        return config

    def reset_to_default(self, tenant_id: str) -> DashBeamRelayConfig:
        """Reset tenant to default public relay."""
        config = self._default_config(tenant_id)
        self._configs[tenant_id] = config
        return config

    def list_custom_relays(self) -> list[DashBeamRelayConfig]:
        """List all custom relay configurations."""
        return [c for c in self._configs.values() if c.is_custom]

    def health_check(self, relay_url: str) -> bool:
        """Check if a relay URL is reachable. Stub — always returns True."""
        return True

    # ─── Internal ───

    def _default_config(self, tenant_id: str) -> DashBeamRelayConfig:
        return DashBeamRelayConfig(
            config_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            relay_url=DEFAULT_RELAY,
            relay_region="auto",
            is_custom=False,
            max_bandwidth_mbps=100,
            discovery_nodes=DEFAULT_DISCOVERY,
        )
