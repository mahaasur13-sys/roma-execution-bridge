"""Crypto Payments — Wallet domain models (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class WalletType(StrEnum):
    PROVIDER = "provider"
    SELF_HOSTED = "self_hosted"
    HARDWARE = "hardware"
    MONERO = "monero"


class WalletMode(StrEnum):
    HOT = "hot"
    VIEW_ONLY = "view_only"
    COLD = "cold"


class WalletStatus(StrEnum):
    ACTIVE = "active"
    ROTATING = "rotating"
    RETIRED = "retired"
    COMPROMISED = "compromised"


class CryptoWallet(BaseModel):
    wallet_id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    label: str
    wallet_type: WalletType
    mode: WalletMode
    provider: str | None = None
    public_address: str | None = None
    rpc_endpoint: str | None = None
    view_key: str | None = None
    status: WalletStatus = WalletStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    rotated_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)


class MoneroViewOnlyConfig(BaseModel):
    primary_address: str
    view_key_private: str
    rpc_url: str = "http://127.0.0.1:18082/json_rpc"
    wallet_rpc_url: str = "http://127.0.0.1:18084/json_rpc"
    poll_interval_seconds: int = 30
    min_confirmations: int = 10
    subaddress_index: int = 0


class MoneroSubaddress(BaseModel):
    wallet_id: UUID
    account_index: int
    subaddress_index: int
    address: str
    label: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DepositAddress(BaseModel):
    address_id: UUID = Field(default_factory=uuid4)
    wallet_id: UUID
    invoice_id: UUID | None = None
    currency: str
    network: str
    address: str
    subaddress_index: int | None = None
    is_monero_subaddress: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    used: bool = False


class WalletRotationEvent(BaseModel):
    rotation_id: UUID = Field(default_factory=uuid4)
    wallet_id: UUID
    tenant_id: str
    old_address: str | None = None
    new_address: str | None = None
    reason: str
    rotated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    performed_by: str = "system"


class CreateWalletRequest(BaseModel):
    tenant_id: str
    label: str
    wallet_type: WalletType
    mode: WalletMode = WalletMode.VIEW_ONLY
    provider: str | None = None
    public_address: str | None = None
    rpc_endpoint: str | None = None
    view_key: str | None = None
    monero_config: MoneroViewOnlyConfig | None = None


class CreateWalletResponse(BaseModel):
    wallet_id: UUID
    tenant_id: str
    label: str
    wallet_type: WalletType
    mode: WalletMode
    status: WalletStatus
    created_at: datetime


class GenerateAddressRequest(BaseModel):
    currency: str
    network: str
    invoice_id: UUID | None = None
    ttl_seconds: int | None = None


class GenerateAddressResponse(BaseModel):
    address_id: UUID
    wallet_id: UUID
    address: str
    currency: str
    network: str
    is_monero_subaddress: bool
    subaddress_index: int | None
    created_at: datetime


class GenerateMoneroSubaddressRequest(BaseModel):
    account_index: int = 0
    label: str | None = None
    invoice_id: UUID | None = None


class GenerateMoneroSubaddressResponse(BaseModel):
    subaddress_index: int
    address: str
    account_index: int
    address_id: UUID
    wallet_id: UUID


class RotateWalletRequest(BaseModel):
    reason: str = "manual_rotation"
    new_public_address: str | None = None


class RotateWalletResponse(BaseModel):
    wallet_id: UUID
    old_status: WalletStatus
    new_wallet_id: UUID | None
    rotation_id: UUID
    message: str
