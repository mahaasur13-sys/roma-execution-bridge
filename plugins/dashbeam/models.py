"""SQLAlchemy models for dashbeam-transfer plugin."""
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class DashBeamTicketRecord(Base):
    __tablename__ = "dashbeam_tickets"

    id: str = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: str = Column(String(36), nullable=False, index=True)
    session_id: str = Column(String(36), nullable=False, index=True)
    ticket_token: str = Column(String(256), nullable=False, unique=True)
    file_name: str = Column(String(256), nullable=False)
    file_size_bytes: int = Column(Integer, nullable=False)
    file_hash: str = Column(String(128), nullable=False)
    mime_type: str = Column(String(128), default="application/octet-stream")
    relay_url: str = Column(String(512), nullable=False)
    peer_node_id: str = Column(String(128), nullable=True)
    status: str = Column(String(32), default="pending", index=True)  # pending, active, completed, expired, failed
    is_one_time: bool = Column(Boolean, default=True)
    expires_at: DateTime = Column(DateTime, nullable=False)
    created_at: DateTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at: Optional[DateTime] = Column(DateTime, nullable=True)


class DashBeamSessionRecord(Base):
    __tablename__ = "dashbeam_sessions"

    id: str = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: str = Column(String(36), nullable=False, index=True)
    peer_node_id: str = Column(String(128), nullable=False)
    device_name: str = Column(String(256), default="")
    device_fingerprint: str = Column(String(256), nullable=True)
    relay_url: str = Column(String(512), nullable=False)
    connected_at: DateTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_heartbeat: DateTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_active: bool = Column(Boolean, default=True)
    bytes_transferred: int = Column(Integer, default=0)
    transfer_count: int = Column(Integer, default=0)


class DashBeamRelayConfigRecord(Base):
    __tablename__ = "dashbeam_relay_configs"

    id: str = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: str = Column(String(36), nullable=False, index=True, unique=True)
    relay_url: str = Column(String(512), nullable=False)
    relay_region: str = Column(String(64), default="auto")
    is_custom: bool = Column(Boolean, default=False)
    max_bandwidth_mbps: int = Column(Integer, default=100)
    discovery_nodes: JSON = Column(JSON, default=list)
    created_at: DateTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: DateTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PairedDeviceRecord(Base):
    __tablename__ = "dashbeam_paired_devices"

    id: str = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: str = Column(String(36), nullable=False, index=True)
    device_name: str = Column(String(256), nullable=False)
    device_fingerprint: str = Column(String(256), nullable=False, unique=True)
    peer_node_id: str = Column(String(128), nullable=True)
    public_key_hash: str = Column(String(128), nullable=True)
    is_trusted: bool = Column(Boolean, default=False)
    paired_at: DateTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen: DateTime = Column(DateTime, default=lambda: datetime.now(timezone.utc))
