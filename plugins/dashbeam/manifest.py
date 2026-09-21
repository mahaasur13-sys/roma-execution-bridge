"""dashbeam-transfer plugin manifest for DecisionOS v1.0.0."""

from plugins.domain.plugin import PluginManifest, PluginCategory, PluginTier

MANIFEST = PluginManifest(
    name="dashbeam-transfer",
    version="1.0.0",
    display_name="DashBeam P2P Transfer",
    description="Secure P2P file transfer via Iroh/QUIC/TLS 1.3 — no cloud storage, no server-side content exposure.",
    author="ROMA Community",
    category=PluginCategory.INTEGRATION,
    entry_point="plugins.dashbeam.service:DashBeamTransferService",
    dependencies=("support-chat", "crypto-wallets"),
    minimum_tier=PluginTier.FREE,
    config_schema={
        "type": "object",
        "properties": {
            "relay_url": {"type": "string", "default": "https://relay.dashbeam.io"},
            "ticket_ttl_minutes": {"type": "integer", "default": 30},
            "max_file_size_mb": {"type": "integer", "default": 500},
            "iroh_data_dir": {"type": "string", "default": "./data/dashbeam"},
            "enable_monero_share": {"type": "boolean", "default": True},
            "block_spend_keys": {"type": "boolean", "default": True},
        },
    },
    permissions=("network", "fs"),
    sandbox_policy="network",
    tags=("p2p", "transfer", "encrypted", "dashbeam", "iroh", "quic", "monero"),
)
