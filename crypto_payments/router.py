"""Crypto Payments — FastAPI router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from crypto_payments.models import (
    CreateInvoiceRequest,
    CreateInvoiceResponse,
    InvoiceStatusResponse,
)
from crypto_payments.service import CryptoInvoiceService
from crypto_payments.settings import CryptoSettings
from crypto_payments.webhooks import CryptoWebhookHandler
from crypto_payments.provider import NOWPaymentsProvider

router = APIRouter(prefix="/v1/crypto", tags=["crypto_payments"])


def _get_service(request: Request) -> CryptoInvoiceService:
    return request.app.state.crypto_service  # type: ignore[attr-defined]


def _get_webhook_handler(request: Request) -> CryptoWebhookHandler:
    return request.app.state.crypto_webhook_handler  # type: ignore[attr-defined]


def _get_settings(request: Request) -> CryptoSettings:
    return request.app.state.crypto_settings  # type: ignore[attr-defined]


@router.post("/invoices", response_model=CreateInvoiceResponse)
async def create_invoice(
    req: CreateInvoiceRequest,
    svc: CryptoInvoiceService = Depends(_get_service),
) -> CreateInvoiceResponse:
    return await svc.create_invoice(req)


@router.get("/invoices/{invoice_id}", response_model=InvoiceStatusResponse)
async def get_invoice(
    invoice_id: str,
    svc: CryptoInvoiceService = Depends(_get_service),
) -> InvoiceStatusResponse:
    return await svc.get_invoice(invoice_id)


@router.post("/webhooks/nowpayments")
async def nowpayments_webhook(
    request: Request,
    handler: CryptoWebhookHandler = Depends(_get_webhook_handler),
    settings: CryptoSettings = Depends(_get_settings),
) -> dict[str, str]:
    raw = await request.body()
    signature = request.headers.get("x-nowpayments-sig", "")
    settings.nowpayments_ipn_secret
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")
    provider = NOWPaymentsProvider(settings)
    if not provider.verify_webhook(raw, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    await handler.process(getattr(provider, "_http", None), raw, settings)
    return {"status": "ok"}
