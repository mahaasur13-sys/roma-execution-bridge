"""Webhook routes extracted from main.py (A1).

CloudPayments + SendGrid webhooks. Moved verbatim from ``main.py`` — no
path/method/status-code changes. Dependencies (``db``, ``limiter``,
``CLOUDPAYMENTS_ENABLED``, ``cloudpayments_client``) are imported from the same
sources ``main.py`` uses, so behaviour is identical.
"""

from __future__ import annotations

import json
import logging

import db_adapter as db
from fastapi import APIRouter, HTTPException, Request

# main.py singletons. ``limiter`` is needed at import time (decorator); the
# CloudPayments client / flag are read via ``main.*`` at request time so test
# monkeypatching of ``main.cloudpayments_client`` keeps working.
import main
from main import limiter

logger = logging.getLogger("roma")

router = APIRouter(tags=["webhooks"])


@limiter.limit("20/minute")
@router.post("/webhooks/cloudpayments")
async def cloudpayments_webhook(request: Request):
    raw_body = await request.body()
    signature = (
        request.headers.get("Content-HMAC")
        or request.headers.get("Content-Hmac")
        or request.headers.get("X-Content-HMAC")
        or ""
    )

    if not main.CLOUDPAYMENTS_ENABLED or main.cloudpayments_client is None:
        return {"code": 0}

    # Fail closed: the webhook secret is mandatory and is NOT the API secret.
    if not (main.cloudpayments_client.config.webhook_secret or "").strip():
        logger.error("cloudpayments_webhook: webhook secret not configured")
        raise HTTPException(status_code=500, detail="webhook secret not configured")

    if not main.cloudpayments_client.verify_webhook(raw_body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("OperationType") or payload.get("Status") or ""
    tenant_id = (payload.get("AccountId") or "").strip()
    invoice_id = payload.get("InvoiceId", "")
    data = payload.get("Data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = {}
    plan = data.get("plan", "")

    if not invoice_id:
        return {"code": 0}

    # Idempotency before any tenant resolution/credit.
    if db.is_invoice_processed(invoice_id):
        return {"code": 0}

    # Tenant is resolved ONLY from AccountId and must already exist.
    if not tenant_id or not db.get_tenant(tenant_id):
        logger.warning(
            "cloudpayments_webhook: unknown AccountId invoice=%s", invoice_id
        )
        raise HTTPException(status_code=422, detail="Unknown AccountId")

    status = payload.get("Status", "")

    try:
        if event_type in ("Payment", "Pay", "Completed") or status in (
            "Completed",
            "Authorized",
        ):
            if plan in ("pro", "enterprise"):
                db.update_tenant_subscription(
                    tenant_id, invoice_id, "", "active", plan, None
                )
                db.mark_invoice_processed(invoice_id, event_type, tenant_id)
            else:
                db.mark_invoice_processed(
                    invoice_id, event_type or "payment_no_plan", tenant_id
                )

        elif event_type == "Recurrent" and status == "Completed":
            if plan:
                db.update_tenant_subscription(
                    tenant_id, invoice_id, "", "active", plan, None
                )
                db.mark_invoice_processed(invoice_id, event_type, tenant_id)
            else:
                db.mark_invoice_processed(
                    invoice_id, event_type or "recurrent_no_plan", tenant_id
                )

        elif event_type in ("Fail", "Declined") or status in ("Declined", "Cancelled"):
            db.set_tenant_inactive(tenant_id)
            db.mark_invoice_processed(invoice_id, event_type, tenant_id)

        elif event_type in ("Cancel", "Unsubscribe"):
            db.update_tenant_subscription(tenant_id, "", "", "canceled", "free", None)
            db.mark_invoice_processed(invoice_id, event_type, tenant_id)

        else:
            db.mark_invoice_processed(invoice_id, event_type or "ignored", tenant_id)

    except Exception:
        logger.exception("Failed to process CloudPayments webhook %s", invoice_id)
        raise HTTPException(status_code=500, detail="Processing error")

    return {"code": 0}


@limiter.limit("20/minute")
@router.post("/webhooks/email")
async def sendgrid_webhook(request: Request):
    """Receive SendGrid event notifications.
    Events: delivered, open, click, bounce, dropped, spamreport.
    See: https://docs.sendgrid.com/for-developers/tracking-events/event
    """
    try:
        events = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(events, list):
        events = [events]

    processed = 0
    for evt in events:
        try:
            event_type = evt.get("event", "")
            email = evt.get("email", "")
            if not email or not event_type:
                continue
            db.update_email_event(email, event_type)
            processed += 1
        except Exception as e:
            logger.warning(f"SendGrid webhook event skipped: {e}")

    logger.info(f"SendGrid webhook: processed {processed}/{len(events)} events")
    return {"status": "ok", "processed": processed}
