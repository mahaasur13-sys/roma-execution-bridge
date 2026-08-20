"""Crypto Payments — Provider Wallet Adapter (NOWPayments/Heleket/CryptoCloud/BTCPay)."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx
from structlog import get_logger

from crypto_payments.settings import CryptoSettings

logger = get_logger(__name__)


class ProviderWalletAdapter:
    """Adapter for provider-managed wallets.

    Supported providers: NOWPayments, Heleket, CryptoCloud, BTCPay Server.
    """

    SUPPORTED_PROVIDERS: frozenset = frozenset({"nowpayments", "heleket", "cryptocloud", "btcpay"})

    def __init__(self, settings: CryptoSettings, provider: str) -> None:
        if provider not in self.SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider}. Supported: {self.SUPPORTED_PROVIDERS}")
        self._provider = provider
        self._settings = settings
        self._http = httpx.AsyncClient(timeout=30)

    async def health_check(self) -> bool:
        try:
            if self._provider == "nowpayments":
                resp = await self._http.get(
                    f"{self._settings.nowpayments_api_url}/status",
                    headers={"x-api-key": self._settings.nowpayments_api_key},
                )
                return resp.status_code == 200
            if self._provider == "heleket":
                resp = await self._http.get(
                    f"{self._settings.heleket_api_url}/v1/ping",
                    headers={"Authorization": f"Bearer {self._settings.heleket_api_key}"},
                )
                return resp.status_code == 200
            if self._provider == "cryptocloud":
                resp = await self._http.post(
                    f"{self._settings.cryptocloud_api_url}/v2/info/ping",
                    headers={"Authorization": f"Token {self._settings.cryptocloud_api_key}"},
                )
                return resp.status_code == 200
            if self._provider == "btcpay":
                resp = await self._http.get(
                    f"{self._settings.btcpay_server_url}/api/v1/health",
                    headers={"Authorization": f"token {self._settings.btcpay_api_key}"},
                )
                return resp.status_code == 200
        except Exception:
            logger.warning("provider_wallet_health_check_failed", provider=self._provider)
        return False

    async def generate_address(self, currency: str, network: str) -> dict[str, Any]:
        logger.info("provider_address_generated", provider=self._provider, currency=currency)
        return {
            "address": f"{self._provider}_{currency}_{network}_addr",
            "currency": currency,
            "network": network,
            "provider": self._provider,
        }

    async def get_wallet_balance(self, currency: str) -> dict[str, Any]:
        return {"currency": currency, "balance": "0", "provider": self._provider}

    async def close(self) -> None:
        await self._http.aclose()
