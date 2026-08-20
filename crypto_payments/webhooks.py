"""Crypto Payments — webhook handler."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from structlog import get_logger

from crypto_payments.models import InvoiceStatus
from crypto_payments.models import CryptoWebhookEvent, InvoiceStatus
from crypto_payments.provider import CryptoPaymentProvider
from crypto_payments.service import CryptoInvoiceService

logger = get_logger(__name__)

WEBHOOK_FINAL_STATUSES: frozenset = frozenset({"finished", "confirmed", "complete"})
WEBHOOK_PARTIAL_STATUSES: frozenset = frozenset({"partially_paid"})
WEBHOOK_TERMINAL_KO: frozenset = frozenset({"expired", "failed", "cancelled"})


class CryptoWebhookHandler:
    def __init__(self, provider: CryptoPaymentProvider, service: CryptoInvoiceService) -> None:
        self._provider = provider
        self._service = service

    async def handle(self, payload: bytes, signature: str) -> dict:
        if not self._provider.verify_webhook(payload, signature):
            logger.warning("crypto_webhook_invalid_signature")
            raise ValueError("Invalid webhook signature")

        data: dict = json.loads(payload)
        payment_status: str = data.get("payment_status", "")
        order_id: str = str(data.get("order_id", ""))

        if payment_status in WEBHOOK_FINAL_STATUSES:
            event = CryptoWebhookEvent(
                provider="nowpayments",
                event_type="payment",
                invoice_id=uuid.UUID(order_id) if order_id else None,
                raw_payload=data,
                status=InvoiceStatus.PAID,
                received_at=datetime.now(timezone.utc),
            )
            await self._service.process_webhook(event)
            logger.info("crypto_payment_confirmed", invoice_id=order_id)

        elif payment_status in WEBHOOK_PARTIAL_STATUSES:
            logger.info("crypto_payment_partial", invoice_id=order_id)

        elif payment_status in WEBHOOK_TERMINAL_KO:
            logger.info("crypto_payment_failed", invoice_id=order_id, status=payment_status)

        return {"status": "processed", "payment_status": payment_status}
