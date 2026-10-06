"""Crypto Payments — provider abstraction + NOWPayments implementation."""

from __future__ import annotations

import hashlib
import hmac
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx
from structlog import get_logger

from crypto_payments.settings import CryptoSettings

logger = get_logger(__name__)


class CryptoPaymentProvider(ABC):
    @abstractmethod
    async def create_invoice(
        self,
        amount_usd: float,
        currency: str,
        network: str,
        order_id: str,
        ttl_minutes: int = 60,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_invoice(self, provider_invoice_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def verify_webhook(self, payload: bytes, signature: str) -> bool: ...


class NOWPaymentsProvider(CryptoPaymentProvider):
    def __init__(self, settings: CryptoSettings | None = None) -> None:
        self._settings = settings or CryptoSettings()
        self._http = httpx.AsyncClient(
            base_url=self._settings.nowpayments_api_url,
            headers={"x-api-key": self._settings.nowpayments_api_key},
            timeout=30,
        )

    async def create_invoice(
        self,
        amount_usd: float,
        currency: str,
        network: str,
        order_id: str,
        ttl_minutes: int = 60,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "price_amount": amount_usd,
            "price_currency": "usd",
            "pay_currency": currency.lower(),
            "order_id": order_id,
            "order_description": f"DecisionOS {network} invoice {order_id}",
        }
        if self._settings.force_network:
            payload["network"] = self._settings.force_network
        resp = await self._http.post("/invoice", json=payload)
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "nowpayments_invoice_created", order_id=order_id, invoice_id=data.get("id")
        )
        return {
            "provider_invoice_id": str(data["id"]),
            "pay_address": data["pay_address"],
            "amount_crypto": float(data["pay_amount"]),
            "currency": data["pay_currency"],
            "network": network,
            "expires_at": datetime.utcnow(),
        }

    async def get_invoice(self, provider_invoice_id: str) -> dict[str, Any]:
        resp = await self._http.get(f"/invoice/{provider_invoice_id}")
        resp.raise_for_status()
        return resp.json()

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        expected = hmac.new(
            self._settings.nowpayments_ipn_secret.encode(),
            payload,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def close(self) -> None:
        await self._http.aclose()
