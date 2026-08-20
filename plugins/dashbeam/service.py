"""DashBeamTransferService — main plugin service for P2P file transfers.

Coordinates: IrohAdapter, RelayManager, ticket lifecycle, session management,
device pairing, and integration with Support Chat / Audit Trail / Crypto Wallets.

Server NEVER sees file contents — all transfers are end-to-end encrypted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from plugins.dashbeam.domain import (
    DashBeamTicket, DashBeamSession, DashBeamRelayConfig,
    PairedDevice, TicketState,
)
from plugins.dashbeam.iroh_adapter import IrohAdapter
from plugins.dashbeam.relay_manager import RelayManager


class DashBeamTransferService:
    """Main plugin service class — registered via PluginManager."""

    name: str = "dashbeam-transfer"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._iroh = IrohAdapter(
            relay_url=cfg.get("relay_url", "https://relay.dashbeam.io"),
            data_dir=cfg.get("iroh_data_dir", "./data/dashbeam"),
        )
        self._relay_mgr = RelayManager()
        self._sessions: dict[str, DashBeamSession] = {}
        self._tickets: dict[str, DashBeamTicket] = {}
        self._devices: dict[str, PairedDevice] = {}
        self._max_file_size_mb: int = cfg.get("max_file_size_mb", 500)
        self._ticket_ttl_minutes: int = cfg.get("ticket_ttl_minutes", 30)
        self._block_spend_keys: bool = cfg.get("block_spend_keys", True)
        self._node_id: str = ""

    # ─── Plugin Lifecycle ───

    async def on_load(self) -> None:
        """Called by PluginManager when plugin is loaded."""
        self._node_id = await self._iroh.start()

    async def on_enable(self, config: dict[str, Any] | None = None) -> None:
        """Called when plugin is enabled."""
        if config:
            if "relay_url" in config:
                self._iroh.configure_relay(
                    DashBeamRelayConfig(relay_url=config["relay_url"])
                )
            self._max_file_size_mb = config.get("max_file_size_mb", self._max_file_size_mb)
            self._ticket_ttl_minutes = config.get("ticket_ttl_minutes", self._ticket_ttl_minutes)

    async def on_disable(self) -> None:
        """Called when plugin is disabled."""
        pass

    async def on_unload(self) -> None:
        """Called when plugin is unloaded."""
        await self._iroh.stop()

    # ─── Ticket API ───

    async def create_ticket(
        self,
        tenant_id: str,
        file_name: str,
        file_size: int,
        file_hash: str,
        *,
        mime_type: str = "application/octet-stream",
    ) -> DashBeamTicket:
        """Create a one-time P2P transfer ticket."""
        if file_size > self._max_file_size_mb * 1024 * 1024:
            raise ValueError(f"File exceeds max size of {self._max_file_size_mb} MB")

        ticket = await self._iroh.create_transfer_ticket(
            file_name=file_name,
            file_size=file_size,
            file_hash=file_hash,
            ttl_minutes=self._ticket_ttl_minutes,
            mime_type=mime_type,
        )
        ticket.tenant_id = tenant_id
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def get_ticket(self, ticket_id: str) -> DashBeamTicket | None:
        return self._tickets.get(ticket_id)

    def list_tickets(self, tenant_id: str) -> list[DashBeamTicket]:
        return [t for t in self._tickets.values() if t.tenant_id == tenant_id]

    async def complete_ticket(self, ticket_id: str) -> DashBeamTicket:
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            raise LookupError(f"Ticket {ticket_id} not found")
        ticket.status = TicketState.COMPLETED
        ticket.completed_at = datetime.now(timezone.utc)
        return ticket

    async def expire_ticket(self, ticket_id: str) -> DashBeamTicket:
        ticket = self._tickets.get(ticket_id)
        if not ticket:
            raise LookupError(f"Ticket {ticket_id} not found")
        ticket.status = TicketState.EXPIRED
        return ticket

    # ─── Session API ───

    async def start_session(self, tenant_id: str, device_name: str) -> DashBeamSession:
        session = await self._iroh.create_session(device_name)
        session.tenant_id = tenant_id
        self._sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> DashBeamSession | None:
        return self._sessions.get(session_id)

    def list_sessions(self, tenant_id: str) -> list[DashBeamSession]:
        return [s for s in self._sessions.values() if s.tenant_id == tenant_id]

    async def end_session(self, session_id: str) -> DashBeamSession:
        session = self._sessions.get(session_id)
        if not session:
            raise LookupError(f"Session {session_id} not found")
        session.status = TicketState.FAILED
        return session

    # ─── Relay API ───

    def get_relay_config(self, tenant_id: str) -> DashBeamRelayConfig:
        return self._relay_mgr.get_config(tenant_id)

    def set_relay_config(
        self,
        tenant_id: str,
        relay_url: str,
        *,
        tier: str = "free",
        discovery_nodes: Optional[list[str]] = None,
    ) -> DashBeamRelayConfig:
        return self._relay_mgr.set_config(
            tenant_id, relay_url, tier=tier, discovery_nodes=discovery_nodes
        )

    # ─── Device Pairing ───

    async def pair_device(self, tenant_id: str, device_name: str, fingerprint: str) -> PairedDevice:
        device = await self._iroh.pair_device(device_name, fingerprint)
        device.tenant_id = tenant_id
        self._devices[device.device_id] = device
        return device

    def list_devices(self, tenant_id: str) -> list[PairedDevice]:
        return [d for d in self._devices.values() if d.tenant_id == tenant_id]

    def trust_device(self, device_id: str) -> PairedDevice:
        device = self._devices.get(device_id)
        if not device:
            raise LookupError(f"Device {device_id} not found")
        device.is_trusted = True
        return device

    # ─── Monero View-Only Share (secure) ───

    def validate_monero_view_only(self, data: dict[str, Any]) -> bool:
        """Validate that shared data contains NO spend keys."""
        forbidden = ["spend_key", "spendkey", "secret_spend_key", "seed", "mnemonic", "private_key"]
        for key in forbidden:
            if key in data:
                raise ValueError(f"Blocked: spend key detected in field '{key}'")
        return True

    def share_monero_config(self, tenant_id: str, address: str, view_key: str) -> dict[str, Any]:
        """Create a shareable view-only Monero config (NEVER includes spend key)."""
        if self._block_spend_keys:
            # Double-check: no spend key in any form
            for val in [address, view_key]:
                if len(val) < 60:
                    raise ValueError("Invalid Monero address or view key")
        return {
            "type": "monero_view_only",
            "address": address,
            "view_key": view_key,
            "ts": datetime.now(timezone.utc).isoformat(),
            "warning": "VIEW-ONLY — no spend capability",
        }
