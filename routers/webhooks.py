"""Webhook routes extracted from main.py (A1).

CloudPayments + SendGrid webhooks. Moved verbatim from ``main.py`` — no
path/method/status-code changes. Dependencies (``db``, ``limiter``,
``CLOUDPAYMENTS_ENABLED``, ``cloudpayments_client``) are imported from the same
sources ``main.py`` uses, so behaviour is identical.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

import db_adapter as db
from fastapi import APIRouter, HTTPException, Request

# main.py singletons. ``limiter`` is needed at import time (decorator); the
# CloudPayments client / flag are read via ``main.*`` at request time so test
# monkeypatching of ``main.cloudpayments_client`` keeps working.
import main
from main import limiter

logger = logging.getLogger("roma")

router = APIRouter(tags=["webhooks"])

# CloudPayments statuses that mean the payment did NOT settle. CloudPayments
# reports a refusal as OperationType=Payment + Status=Declined, so the
# operation type alone must never be read as "paid" (F-001). The tuple is
# shared with the Fail branch below so the two cannot drift apart.
_REFUSED_STATUSES = ("Declined", "Cancelled")

# F-004: the amount AND the currency CloudPayments reports must match the plan
# before any credit/activation. Compared as exact ``Decimal`` values — the old
# float tolerance is gone, so a drifted kopeck is a mismatch, not rounding.
def _expected_plan_money(plan: str) -> tuple[Decimal, str] | None:
    """Plan price + currency from ``main.CLOUDPAYMENTS_PLANS``; None if unknown."""
    plans = getattr(main, "CLOUDPAYMENTS_PLANS", None) or {}
    try:
        amount = Decimal(str(plans[plan]["amount"]))
        currency = str(plans[plan]["currency"] or "").strip().upper()
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not amount.is_finite() or not currency:
        return None
    return amount, currency


def _amount_matches_plan(payload: dict, plan: str) -> bool:
    """Fail closed on missing, unparsable or mismatching Amount or Currency.

    The currency is checked against the plan's currency and is never assumed:
    a missing, empty, non-string or unknown currency is a mismatch.
    """
    expected = _expected_plan_money(plan)
    if expected is None:
        return False
    expected_amount, expected_currency = expected

    raw_currency = payload.get("Currency")
    if not isinstance(raw_currency, str) or not raw_currency.strip():
        return False
    if raw_currency.strip().upper() != expected_currency:
        return False

    raw = payload.get("Amount")
    if raw is None or isinstance(raw, bool):
        return False
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return False
    if not amount.is_finite():
        return False
    return amount == expected_amount


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

    # Tenant is resolved ONLY from AccountId and must already exist (F-005:
    # this gate runs BEFORE idempotency, so a foreign or unresolved AccountId
    # can never be answered with a silent {"code": 0}).
    if not tenant_id or not db.get_tenant(tenant_id):
        logger.warning(
            "cloudpayments_webhook: unknown AccountId invoice=%s", invoice_id
        )
        raise HTTPException(status_code=422, detail="Unknown AccountId")

    # Idempotency is scoped to the resolved tenant (F-005): an InvoiceId already
    # recorded for another tenant must not silence this tenant's event.
    if db.is_invoice_processed(invoice_id, tenant_id):
        return {"code": 0}
    if db.is_invoice_processed(invoice_id):
        logger.warning(
            "cloudpayments_webhook: invoice recorded for another tenant "
            "invoice=%s account=%s",
            invoice_id,
            tenant_id,
        )
        raise HTTPException(
            status_code=409, detail="Invoice belongs to another tenant"
        )

    status = payload.get("Status", "")
    refused = status in _REFUSED_STATUSES

    try:
        if not refused and (
            event_type in ("Payment", "Pay", "Completed")
            or status in ("Completed", "Authorized")
        ):
            if plan in ("pro", "enterprise"):
                # F-004: never activate a tenant on an amount that does not
                # match the plan price. Fail closed on a missing Amount.
                if not _amount_matches_plan(payload, plan):
                    # Deliberately NOT marked processed (F-004): the invoice
                    # is left open so a corrected notification for the same
                    # InvoiceId can still activate. Logged on every mismatch,
                    # so a retry loop resending the wrong amount stays visible.
                    logger.warning(
                        "cloudpayments_webhook: amount does not match plan — "
                        "not marked processed, invoice left open for a "
                        "corrected retry invoice=%s account=%s plan=%s",
                        invoice_id,
                        tenant_id,
                        plan,
                    )
                    return {"code": 0}
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
                # F-004: recurrent charges are checked against the plan too.
                if not _amount_matches_plan(payload, plan):
                    # Same F-004 rule for recurrent charges: not marked
                    # processed, so the invoice stays open for a corrected
                    # retry, and every mismatch is logged.
                    logger.warning(
                        "cloudpayments_webhook: recurrent amount does not "
                        "match plan — not marked processed, invoice left open "
                        "for a corrected retry invoice=%s account=%s plan=%s",
                        invoice_id,
                        tenant_id,
                        plan,
                    )
                    return {"code": 0}
                db.update_tenant_subscription(
                    tenant_id, invoice_id, "", "active", plan, None
                )
                db.mark_invoice_processed(invoice_id, event_type, tenant_id)
            else:
                db.mark_invoice_processed(
                    invoice_id, event_type or "recurrent_no_plan", tenant_id
                )

        elif event_type in ("Fail", "Declined") or refused:
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
