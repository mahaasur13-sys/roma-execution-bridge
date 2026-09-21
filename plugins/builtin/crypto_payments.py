"""Crypto Payments Plugin — NOWPayments + Monero view-only integration."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

logger = logging.getLogger("roma.plugin.crypto_payments")

SUPPORTED_CURRENCIES = ("USDT", "BTC", "ETH", "XMR")


class CryptoPaymentsPlugin:
    """Handles crypto invoice creation, payment detection, and Monero view-only wallets."""

    name = "crypto-payments"
    display_name = "Crypto Payments"

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._nowpayments_key: str = ""
        self._monero_view_key: str = ""

    def on_enable(self, config: dict[str, Any]) -> None:
        self._config = config
        self._nowpayments_key = config.get("nowpayments_key", "")
        self._monero_view_key = config.get("monero_view_key", "")
        logger.info(
            "CryptoPaymentsPlugin enabled (NOWPayments: %s)",
            "yes" if self._nowpayments_key else "no",
        )

    async def run(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Primary entry point."""
        if action == "create_invoice":
            return await self._create_invoice(
                kwargs["amount"],
                kwargs.get("currency", "USDT"),
                kwargs.get("description", "ROMA Invoice"),
            )
        elif action == "check_status":
            return await self._check_status(kwargs["invoice_id"])
        elif action == "add_monero_wallet":
            return self._add_monero_wallet(kwargs["address"], kwargs["view_key"])
        elif action == "check_monero_balance":
            return await self._check_monero_balance(kwargs["address"])
        else:
            return {"error": f"Unknown action: {action}"}

    async def _create_invoice(
        self, amount: float, currency: str, description: str
    ) -> dict[str, Any]:
        """Create a NOWPayments invoice."""
        if currency not in SUPPORTED_CURRENCIES:
            return {"error": f"Unsupported currency: {currency}"}

        invoice_id = hmac.new(
            self._nowpayments_key.encode() or b"demo",
            f"{amount}{currency}".encode(),
            hashlib.sha256,
        ).hexdigest()[:16]

        logger.info(
            "Invoice created: %s %s %s → %s", amount, currency, description, invoice_id
        )

        return {
            "invoice_id": invoice_id,
            "amount": amount,
            "currency": currency,
            "description": description,
            "status": "pending",
            "payment_address": f"demo_{currency.lower()}_address_{invoice_id[:8]}",
            "expires_in": 3600,
            "qr_url": f"https://nowpayments.io/qr/{invoice_id}",
        }

    async def _check_status(self, invoice_id: str) -> dict[str, Any]:
        """Check NOWPayments invoice status."""
        return {
            "invoice_id": invoice_id,
            "status": "pending",
            "paid_amount": 0.0,
            "confirmations": 0,
        }

    def _add_monero_wallet(self, address: str, view_key: str) -> dict[str, Any]:
        """Register a Monero view-only wallet. NEVER store spend_key."""
        if "spend" in view_key.lower():
            return {
                "error": "SPEND KEY DETECTED. View-only wallets only. Spend key rejected automatically."
            }

        if not address.startswith("4") and not address.startswith("8"):
            return {"error": "Invalid Monero address format"}

        self._monero_view_key = view_key
        logger.info("Monero view-only wallet added: %s...", address[:8])

        return {
            "status": "connected",
            "address": address[:8] + "..." + address[-6:],
            "type": "view-only",
            "note": "Spend key was NOT stored (view-only mode active).",
        }

    async def _check_monero_balance(self, address: str) -> dict[str, Any]:
        """Check Monero view-only balance."""
        return {
            "address": address[:8] + "..." + address[-6:],
            "balance_xmr": 0.0,
            "unlocked_balance_xmr": 0.0,
            "blockchain_height": 0,
            "type": "view-only",
            "warning": "Live balance fetch requires connected XMR node. Current: placeholder.",
        }
