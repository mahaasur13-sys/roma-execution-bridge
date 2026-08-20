"""DashBeam Transfer Plugin — P2P file transfers for DecisionOS.

Built on Iroh P2P stack: QUIC + TLS 1.3, end-to-end encryption,
device pairing via tickets, custom relay support (Enterprise).

Inspired by DeepSeek Harness / Cordis plugin architecture.
"""

from plugins.dashbeam.manifest import MANIFEST as PLUGIN_MANIFEST
from plugins.dashbeam.service import DashBeamTransferService

__all__ = ["PLUGIN_MANIFEST", "DashBeamTransferService"]


def create_plugin(config=None):
    """Factory function for PluginManager loader."""
    return DashBeamTransferService(config)
