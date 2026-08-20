"""Crypto Payments — Pydantic v2 domain models."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class CryptoCurrency(StrEnum):
    USDT_TRC20 = "USDT_TRC20"
    USDT_ERC20 = "USDT_ERC20"
    USDC = "USDC"
    BTC = "BTC"
    TON = "TON"
    SOL = "SOL"


class CryptoNetwork(StrEnum):
    TRC20 = "TRC20"
    ERC20 = "ERC20"
    BTC = "BTC"
    TON = "TON"
    SOL = "SOL"

    @classmethod
    def from_currency(cls, currency):
        from crypto_payments.models import CryptoCurrency
        mapping = {
            CryptoCurrency.USDT_TRC20: cls.TRC20,
            CryptoCurrency.USDT_ERC20: cls.ERC20,
            CryptoCurrency.USDC: cls.ERC20,
            CryptoCurrency.BTC: cls.BTC,
            CryptoCurrency.TON: cls.TON,
            CryptoCurrency.SOL: cls.SOL,
        }
        return mapping[currency]

NETWORK_FOR_CURRENCY: dict[CryptoCurrency, CryptoNetwork] = {
    CryptoCurrency.USDT_TRC20: CryptoNetwork.TRC20,
    CryptoCurrency.USDT_ERC20: CryptoNetwork.ERC20,
    CryptoCurrency.USDC: CryptoNetwork.ERC20,
    CryptoCurrency.BTC: CryptoNetwork.BTC,
    CryptoCurrency.TON: CryptoNetwork.TON,
    CryptoCurrency.SOL: CryptoNetwork.SOL,
}




class InvoiceStatus(StrEnum):
    PENDING = "pending"
    CONFIRMING = "confirming"
    PAID = "paid"
    OVERPAID = "overpaid"
    UNDERPAID = "underpaid"
    EXPIRED = "expired"
    FAILED = "failed"


class TierName(StrEnum):
    START = "start"
    PRO = "pro"
    ENTERPRISE = "enterprise"


TIER_PRICES_USD: dict[TierName, Decimal] = {
    TierName.START: Decimal("49.00"),
    TierName.PRO: Decimal("149.00"),
    TierName.ENTERPRISE: Decimal("499.00"),
}

TIER_DURATION_MONTHS = 1

PAYMENT_TTL_SECONDS = 3600


class CryptoInvoice(BaseModel):
    invoice_id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    tier: TierName
    currency: CryptoCurrency
    network: CryptoNetwork
    amount_crypto: Decimal | None = None
    amount_usd: Decimal
    pay_address: str | None = None
    status: InvoiceStatus = InvoiceStatus.PENDING
    provider_invoice_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    paid_at: datetime | None = None

    @field_validator("amount_usd")
    @classmethod
    def _must_match_tier(cls, v: Decimal, info: Any) -> Decimal:
        tier = (info.data or {}).get("tier")
        if tier and tier in TIER_PRICES_USD and v != TIER_PRICES_USD[tier]:
            raise ValueError(f"Amount {v} does not match tier price {TIER_PRICES_USD[tier]}")
        return v


class CryptoPayment(BaseModel):
    payment_id: UUID = Field(default_factory=uuid4)
    invoice_id: UUID
    tenant_id: str
    tx_hash: str
    currency: CryptoCurrency
    network: CryptoNetwork
    amount_paid: Decimal
    amount_usd: Decimal
    confirmations: int = 0
    received_at: datetime = Field(default_factory=datetime.utcnow)


class CryptoWebhookEvent(BaseModel):
    event_id: UUID = Field(default_factory=uuid4)
    provider: str
    event_type: str
    raw_payload: dict
    invoice_id: UUID | None = None
    status: InvoiceStatus | None = None
    received_at: datetime = Field(default_factory=datetime.utcnow)


class CreateInvoiceRequest(BaseModel):
    tenant_id: str
    tier: TierName
    currency: CryptoCurrency = CryptoCurrency.USDT_TRC20

    @field_validator("tenant_id")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("tenant_id must not be empty")
        return v.strip()


class CreateInvoiceResponse(BaseModel):
    invoice_id: UUID
    status: InvoiceStatus
    pay_address: str
    amount_crypto: Decimal
    amount_usd: Decimal
    currency: CryptoCurrency
    network: CryptoNetwork
    expires_at: datetime


class InvoiceStatusResponse(BaseModel):
    invoice_id: UUID
    status: InvoiceStatus
    tier: TierName
    amount_crypto: Decimal | None
    amount_usd: Decimal
    pay_address: str | None
    created_at: datetime
    expires_at: datetime
    paid_at: datetime | None
    tx_hash: str | None = None
