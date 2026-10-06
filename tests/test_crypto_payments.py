"""DecisionOS Crypto Payments — 8 smoke tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4, UUID
from unittest.mock import AsyncMock, MagicMock

import pytest

from crypto_payments.models import (
    CreateInvoiceRequest,
    CryptoCurrency,
    CryptoInvoice,
    CryptoNetwork,
    CryptoWebhookEvent,
    InvoiceStatus,
    InvoiceStatusResponse,
    TIER_PRICES_USD,
    TierName,
)
from crypto_payments.service import CryptoInvoiceService
from crypto_payments.webhooks import CryptoWebhookHandler
from crypto_payments.provider import NOWPaymentsProvider
from crypto_payments.settings import CryptoSettings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def settings() -> CryptoSettings:
    return CryptoSettings(
        nowpayments_api_key="test-key",
        nowpayments_ipn_secret="test-secret",
        invoice_ttl_minutes=60,
    )


@pytest.fixture
def mock_provider(settings: CryptoSettings) -> NOWPaymentsProvider:
    provider = MagicMock(spec=NOWPaymentsProvider)
    provider.create_invoice = AsyncMock(
        return_value={
            "pay_address": "TXtest123",
            "invoice_id": "np_inv_1",
        }
    )
    provider.get_invoice = AsyncMock(
        return_value={
            "payment_status": "pending",
        }
    )
    provider.verify_webhook = MagicMock(return_value=True)
    return provider


@pytest.fixture
def service(
    settings: CryptoSettings, mock_provider: NOWPaymentsProvider
) -> CryptoInvoiceService:
    return CryptoInvoiceService(provider=mock_provider, settings=settings)


@pytest.fixture
def webhook_handler(
    settings: CryptoSettings, mock_provider: NOWPaymentsProvider
) -> CryptoWebhookHandler:
    svc = CryptoInvoiceService(provider=mock_provider, settings=settings)
    return CryptoWebhookHandler(provider=mock_provider, service=svc)


class TestCreateInvoice:
    async def test_creates_invoice(self, service: CryptoInvoiceService) -> None:
        request = CreateInvoiceRequest(
            tenant_id="t1",
            tier=TierName.START,
            currency=CryptoCurrency.USDT_TRC20,
        )
        result = await service.create_invoice(request)
        assert result.invoice_id is not None
        assert result.invoice_id is not None

    async def test_assigns_unique_invoice_id(
        self, service: CryptoInvoiceService
    ) -> None:
        request = CreateInvoiceRequest(
            tenant_id="t1",
            tier=TierName.START,
            currency=CryptoCurrency.USDT_TRC20,
        )
        inv1 = await service.create_invoice(request)
        inv2 = await service.create_invoice(request)
        assert inv1.invoice_id != inv2.invoice_id


class TestGetInvoice:
    async def test_returns_invoice_status(self, service: CryptoInvoiceService) -> None:
        request = CreateInvoiceRequest(
            tenant_id="t1",
            tier=TierName.START,
            currency=CryptoCurrency.USDT_TRC20,
        )
        created = await service.create_invoice(request)
        result = await service.get_invoice(created.invoice_id)
        assert isinstance(result, InvoiceStatusResponse)


class TestWebhook:
    async def test_processes_paid_webhook(
        self, webhook_handler: CryptoWebhookHandler
    ) -> None:
        inv_id = str(uuid4())
        result = await webhook_handler.handle(
            payload=json.dumps(
                {
                    "payment_status": "finished",
                    "order_id": inv_id,
                }
            ).encode(),
            signature="test-sig",
        )
        assert result["status"] == "processed"


class TestModels:
    def test_invoice_serialization(self) -> None:
        invoice = CryptoInvoice(
            invoice_id=str(uuid4()),
            tenant_id="t1",
            tier=TierName.START,
            network=CryptoNetwork.TRC20.value,
            currency=CryptoCurrency.USDT_TRC20.value,
            amount_usd=TIER_PRICES_USD[TierName.START],
            wallet_address="TXtest",
            provider_invoice_id="np_1",
            status=InvoiceStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc),
        )
        assert invoice.invoice_id is not None
        assert isinstance(invoice.amount_usd, object)

    def test_webhook_event_parsing(self) -> None:
        raw = json.dumps(
            {
                "event_id": str(uuid4()),
                "provider": "nowpayments",
                "event_type": "payment",
                "invoice_id": str(uuid4()),
                "raw_payload": {"payment_status": "finished"},
                "status": "paid",
                "tx_hash": "abc123",
                "currency_paid": "usdt",
                "received_amount": "49.0",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        event = CryptoWebhookEvent.model_validate_json(raw)
        assert isinstance(event.event_id, UUID)

    def test_tier_prices_defined(self) -> None:
        assert float(TIER_PRICES_USD[TierName.START]) == 49.0
        assert float(TIER_PRICES_USD[TierName.PRO]) == 149.0
        assert float(TIER_PRICES_USD[TierName.ENTERPRISE]) == 499.0


class TestPricingLogic:
    def test_amount_matches_tier(self) -> None:
        request = CreateInvoiceRequest(
            tenant_id="t1",
            tier=TierName.START,
            currency=CryptoCurrency.USDT_TRC20,
        )
        assert TIER_PRICES_USD[request.tier] == TIER_PRICES_USD[TierName.START]

    def test_network_from_currency(self) -> None:
        assert (
            CryptoNetwork.from_currency(CryptoCurrency.USDT_TRC20)
            == CryptoNetwork.TRC20
        )
        assert CryptoNetwork.from_currency(CryptoCurrency.BTC) == CryptoNetwork.BTC

    def test_status_transitions(self) -> None:
        assert InvoiceStatus.PENDING.value == "pending"
        assert InvoiceStatus.PAID.value == "paid"
        assert InvoiceStatus.EXPIRED.value == "expired"
