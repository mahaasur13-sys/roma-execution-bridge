"""DashBeam P2P Transfer Plugin — Domain Entities (Pydantic v2).

DashBeam: secure P2P file transfer via Iroh (QUIC + TLS 1.3).
Server never sees file contents. Tickets are one-time-use.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ────────────────────────────────────────
# Ticket State Machine
# ────────────────────────────────────────

class TicketState(StrEnum):
    """DashBeam ticket lifecycle."""
    CREATED = "created"       # Generated, not yet shared
    PENDING = "pending"       # Shared with recipient, waiting
    ACCEPTED = "accepted"     # Recipient accepted the ticket
    TRANSFERRING = "transferring"  # P2P transfer in progress
    COMPLETED = "completed"   # Transfer finished successfully
    EXPIRED = "expired"       # Ticket expired (TTL exceeded)
    REVOKED = "revoked"       # Sender revoked the ticket
    FAILED = "failed"         # Transfer failed


class TicketType(StrEnum):
    """Purpose of the DashBeam ticket."""
    FILE_TRANSFER = "file_transfer"
    AUDIT_EXPORT = "audit_export"
    WALLET_CONFIG = "wallet_config"  # Monero view-only configs
    GENERAL = "general"


class TicketVisibility(StrEnum):
    """Who can see/use this ticket."""
    TENANT_ONLY = "tenant_only"
    SINGLE_RECIPIENT = "single_recipient"
    PUBLIC_LINK = "public_link"


# ────────────────────────────────────────
# Domain Entities
# ────────────────────────────────────────

class DashBeamTicket(BaseModel):
    """One-time P2P transfer ticket.

    Contains Iroh ticket bytes + metadata. Server stores metadata only;
    the actual Iroh ticket blob is encrypted and opaque to the server.
    """
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    created_by: str  # user_id
    ticket_type: TicketType
    state: TicketState = TicketState.CREATED
    visibility: TicketVisibility = TicketVisibility.SINGLE_RECIPIENT

    # Iroh ticket payload (opaque bytes, base64-encoded for storage)
    iroh_ticket_b64: str = Field(default="", description="Base64-encoded Iroh ticket blob")
    iroh_hash: str = Field(default="", description="SHA-256 hash of the Iroh ticket")

    # Metadata (visible to server for audit)
    file_name: str = Field(default="", max_length=512)
    file_size_bytes: int = 0
    mime_type: str = Field(default="application/octet-stream")
    recipient_id: str = Field(default="")

    # TTL and expiry
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    max_transfers: int = 1  # One-time by default
    transfer_count: int = 0

    # Relay configuration
    relay_url: str = Field(default="")
    relay_token: str = Field(default="")

    # Policy metadata
    policy_action_id: str = Field(default="")
    audit_event_id: str = Field(default="")

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    def is_consumed(self) -> bool:
        return self.transfer_count >= self.max_transfers


class DashBeamSession(BaseModel):
    """Active P2P transfer session between two devices."""
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ticket_id: str
    tenant_id: str
    sender_device_id: str
    recipient_device_id: str = Field(default="")

    # Iroh session state
    iroh_session_id: str = Field(default="")
    quic_connection_id: str = Field(default="")

    state: TicketState = TicketState.PENDING
    bytes_transferred: int = 0
    bytes_total: int = 0

    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str = Field(default="")

    # Encryption verification
    tls_fingerprint: str = Field(default="")
    encryption_verified: bool = False

    def progress_pct(self) -> float:
        if self.bytes_total == 0:
            return 0.0
        return round(self.bytes_transferred / self.bytes_total * 100, 1)


class DashBeamRelayConfig(BaseModel):
    """Relay server configuration — public or custom."""
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    name: str = Field(default="Default Relay")

    # Relay endpoint
    relay_url: str = Field(default="https://relay.dashbeam.io")
    relay_port: int = 443

    # Authentication
    relay_token: str = Field(default="")
    relay_cert_fingerprint: str = Field(default="")

    # Tier restrictions
    is_custom: bool = False  # Enterprise-only for custom relays
    is_active: bool = True

    # Discovery
    discovery_url: str = Field(default="")
    discovery_method: str = Field(default="dns", description="dns | mdns | static")

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PairedDevice(BaseModel):
    """A device paired for P2P transfers."""
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    user_id: str

    device_name: str = Field(default="Unknown Device")
    device_id: str = Field(default="")  # Iroh node ID
    public_key_b64: str = Field(default="")

    paired_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime | None = None
    is_trusted: bool = False

    # Device capabilities
    supports_relay: bool = True
    max_file_size_bytes: int = 0  # 0 = no limit


# ────────────────────────────────────────
# Plugin Manifest
# ────────────────────────────────────────

DASHBEAM_TRANSFER_MANIFEST: dict[str, Any] = {
    "name": "dashbeam-transfer",
    "version": "1.0.0",
    "display_name": "DashBeam P2P Transfer",
    "description": "Secure peer-to-peer file transfers via Iroh (QUIC + TLS 1.3). No cloud storage — files go directly between devices.",
    "author": "ROMA DecisionOS Team",
    "category": "integration",
    "entry_point": "plugins.dashbeam.service:DashBeamTransferService",
    "dependencies": [],
    "minimum_tier": "free",
    "config_schema": {
        "type": "object",
        "properties": {
            "default_relay_url": {"type": "string", "default": "https://relay.dashbeam.io"},
            "default_relay_port": {"type": "integer", "default": 443},
            "ticket_ttl_seconds": {"type": "integer", "default": 3600},
            "max_file_size_bytes": {"type": "integer", "default": 1073741824},
            "require_encryption_verification": {"type": "boolean", "default": True},
            "block_spend_keys": {"type": "boolean", "default": True},
        },
    },
    "permissions": ["network", "fs"],
    "sandbox_policy": "network",
    "tags": ["p2p", "iroh", "dashbeam", "file-transfer", "encryption", "monero"],
}
