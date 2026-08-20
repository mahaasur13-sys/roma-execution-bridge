#!/usr/bin/env python3
"""
ROMA CloudPayments Client — checkout, subscriptions, webhooks.
Replaces billing/stripe_client.py (deprecated).
Docs: https://developers.cloudpayments.ru/
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("roma.cloudpayments")

CLOUDPAYMENTS_API = "https://api.cloudpayments.ru"
CLOUDPAYMENTS_WIDGET = "https://widget.cloudpayments.ru"


@dataclass
class CloudPaymentsConfig:
    public_id: str
    api_secret: str
    webhook_secret: str = ""
    mode: str = "test"  # test | live


def _load_config() -> CloudPaymentsConfig | None:
    public_id = os.environ.get("CLOUDPAYMENTS_PUBLIC_ID", "")
    api_secret = os.environ.get("CLOUDPAYMENTS_API_SECRET", "")
    if not public_id or not api_secret:
        return None
    return CloudPaymentsConfig(
        public_id=public_id,
        api_secret=api_secret,
        webhook_secret=os.environ.get("CLOUDPAYMENTS_WEBHOOK_SECRET", ""),
        mode="test" if "test" in os.environ.get("CLOUDPAYMENTS_MODE", "test") else "live",
    )


class CloudPaymentsClient:
    """Sync client for CloudPayments API."""

    def __init__(self, config: CloudPaymentsConfig) -> None:
        self.config = config
        self._http = httpx.Client(
            base_url=CLOUDPAYMENTS_API,
            timeout=15.0,
            auth=(config.public_id, config.api_secret),
            headers={"Content-Type": "application/json"},
        )

    # ── orders / checkout ──────────────────────────────────────

    def create_order(
        self,
        amount: float,
        currency: str,
        description: str,
        email: str = "",
        require_confirmation: bool = False,
        subscription_plan: str = "",
    ) -> dict[str, Any]:
        """
        Create a one-time order. Returns a dict with:
          - Id: order ID
          - Number: order number
          - Url: hosted payment page URL (user is redirected here)
        """
        payload: dict[str, Any] = {
            "Amount": amount,
            "Currency": currency,
            "Description": description,
            "Email": email,
            "RequireConfirmation": require_confirmation,
        }
        if subscription_plan:
            payload["JsonData"] = json.dumps({"plan": subscription_plan})

        resp = self._post("/orders/create", payload)
        return resp

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._post("/orders/get", {"Id": order_id})

    # ── subscriptions ──────────────────────────────────────────

    def create_subscription(
        self,
        token: str,
        account_id: str,
        description: str,
        amount: float,
        currency: str,
        interval: str = "Month",
        period: int = 1,
        email: str = "",
    ) -> dict[str, Any]:
        """
        Create a recurring subscription from a saved card token.
        Interval: Day, Week, Month
        Period: 1–365
        """
        payload: dict[str, Any] = {
            "Token": token,
            "AccountId": account_id,
            "Description": description,
            "Amount": amount,
            "Currency": currency,
            "Interval": interval,
            "Period": period,
            "Email": email,
        }
        return self._post("/subscriptions/create", payload)

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._post("/subscriptions/get", {"Id": subscription_id})

    def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._post("/subscriptions/cancel", {"Id": subscription_id})

    # ── refund ─────────────────────────────────────────────────

    def refund(self, transaction_id: int, amount: float) -> dict[str, Any]:
        return self._post("/payments/refund", {"TransactionId": transaction_id, "Amount": amount})

    # ── webhook verification ───────────────────────────────────

    def verify_webhook(self, body: bytes, signature_header: str | None) -> bool:
        """
        Verify HMAC-SHA256 webhook signature from CloudPayments.
        Priority: webhook_secret -> api_secret (fallback).
        """
        if not signature_header:
            return False
        secret = (self.config.webhook_secret or self.config.api_secret or "").strip()
        if not secret:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header.strip())

    # ── internal ───────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(path, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("Success", True):
            error_msg = data.get("Message", "Unknown CloudPayments error")
            logger.error(f"CloudPayments API error on {path}: {error_msg}")
            raise CloudPaymentsError(error_msg, data)
        return data.get("Model", data)

    def close(self) -> None:
        self._http.close()


class CloudPaymentsError(Exception):
    def __init__(self, message: str, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response = response


# ── price configuration ────────────────────────────────────────

PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "name": "Free",
        "amount": 0,
        "currency": "RUB",
        "jobs_per_month": 50,
        "gpu_hours": 0,
    },
    "pro": {
        "name": "Pro",
        "amount": 4900,  # ₽4,900/month
        "currency": "RUB",
        "jobs_per_month": 500,
        "gpu_hours": 20,
    },
    "enterprise": {
        "name": "Enterprise",
        "amount": 29900,  # ₽29,900/month
        "currency": "RUB",
        "jobs_per_month": 999_999,
        "gpu_hours": 200,
    },
}
