"""Crypto Payments — service layer."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from structlog import get_logger

from crypto_payments.models import (
    CryptoCurrency,
    CryptoNetwork,
    CreateInvoiceRequest,
    CreateInvoiceResponse,
    CryptoInvoice,
    CryptoPayment,
    CryptoWebhookEvent,
    InvoiceStatus,
    InvoiceStatusResponse,
    NETWORK_FOR_CURRENCY,
    PAYMENT_TTL_SECONDS,
    TIER_PRICES_USD,
    TierName,
)
from crypto_payments.provider import CryptoPaymentProvider
from crypto_payments.settings import CryptoSettings

logger = get_logger(__name__)


class CryptoInvoiceService:
    def __init__(
        self, provider: CryptoPaymentProvider, settings: CryptoSettings
    ) -> None:
        self._provider = provider
        self._settings = settings

    async def create_invoice(
        self, request: CreateInvoiceRequest
    ) -> CreateInvoiceResponse:
        network = NETWORK_FOR_CURRENCY[request.currency]
        amount_usd = TIER_PRICES_USD[request.tier]
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=PAYMENT_TTL_SECONDS)

        invoice = CryptoInvoice(
            tenant_id=request.tenant_id,
            tier=request.tier,
            network=network,
            currency=request.currency,
            amount_usd=amount_usd,
            status=InvoiceStatus.PENDING,
            created_at=now,
            expires_at=expires_at,
        )

        provider_result = await self._provider.create_invoice(
            amount_usd=float(amount_usd),
            currency=invoice.currency.value,
            network=invoice.network.value,
            order_id=str(invoice.invoice_id),
            ttl_minutes=self._settings.invoice_ttl_minutes,
        )

        invoice = invoice.model_copy(
            update={
                "pay_address": provider_result.get("pay_address", ""),
                "provider_invoice_id": provider_result.get("invoice_id", ""),
            }
        )

        logger.info(
            "crypto_invoice_created",
            invoice_id=str(invoice.invoice_id),
            tenant_id=invoice.tenant_id,
        )
        return CreateInvoiceResponse(
            invoice_id=invoice.invoice_id,
            status=invoice.status,
            pay_address=invoice.pay_address or "",
            amount_crypto=invoice.amount_crypto or Decimal("0"),
            amount_usd=invoice.amount_usd,
            currency=invoice.currency,
            network=invoice.network,
            expires_at=invoice.expires_at,
        )

    async def get_invoice(self, invoice_id: str) -> InvoiceStatusResponse:
        invoice = await self._load_invoice(str(invoice_id))
        try:
            provider_data = await self._provider.get_invoice(
                invoice.provider_invoice_id or ""
            )
            status_raw = provider_data.get("payment_status", invoice.status.value)
        except Exception:
            status_raw = invoice.status.value
        status = InvoiceStatus(status_raw)
        return InvoiceStatusResponse(
            invoice_id=invoice.invoice_id,
            status=status,
            tier=invoice.tier,
            amount_crypto=invoice.amount_crypto,
            amount_usd=invoice.amount_usd,
            pay_address=invoice.pay_address,
            created_at=invoice.created_at,
            expires_at=invoice.expires_at,
            paid_at=invoice.paid_at,
        )

    async def process_webhook(self, event: CryptoWebhookEvent) -> None:
        raw = event.raw_payload
        logger.info(
            "crypto_webhook_received",
            invoice_id=str(event.invoice_id),
            status=event.status.value if event.status else "unknown",
        )
        if event.status != InvoiceStatus.PAID:
            return
        invoice = await self._load_invoice(str(event.invoice_id))
        now = datetime.now(timezone.utc)
        tx_hash = str(raw.get("payin_hash", ""))
        invoice = invoice.model_copy(
            update={
                "status": InvoiceStatus.PAID,
                "paid_at": now,
            }
        )
        payment = CryptoPayment(
            invoice_id=invoice.invoice_id,
            tenant_id=invoice.tenant_id,
            tx_hash=tx_hash,
            currency=invoice.currency,
            network=invoice.network,
            amount_paid=Decimal(str(raw.get("actually_paid", "0"))),
            amount_usd=invoice.amount_usd,
        )
        logger.info(
            "crypto_payment_confirmed",
            payment_id=str(payment.payment_id),
            tenant_id=payment.tenant_id,
        )

    async def _load_invoice(self, invoice_id: str) -> CryptoInvoice:
        return CryptoInvoice(
            invoice_id=uuid.UUID(invoice_id),
            tenant_id="demo",
            tier=TierName.START,
            network=CryptoNetwork.TRC20,
            currency=CryptoCurrency.USDT_TRC20,
            amount_usd=TIER_PRICES_USD[TierName.START],
            pay_address="0x0",
            provider_invoice_id="np_demo",
            status=InvoiceStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=PAYMENT_TTL_SECONDS),
        )
