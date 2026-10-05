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

# CloudPayments statuses that mean the payment did NOT settle. CloudPayments
# reports a refusal as OperationType=Payment + Status=Declined, so the
# operation type alone must never be read as "paid" (F-001). The tuple is
# shared with the Fail branch below so the two cannot drift apart.
_REFUSED_STATUSES = ("Declined", "Cancelled")

# F-004: the amount CloudPayments reports must match the plan price before any
# credit/activation. Tolerance covers kopeck rounding only.
_AMOUNT_TOLERANCE = 0.01


def _expected_plan_amount(plan: str) -> float | None:
    """Plan price from ``main.CLOUDPAYMENTS_PLANS``; None if the plan is unknown."""
    plans = getattr(main, "CLOUDPAYMENTS_PLANS", None) or {}
    try:
        return float(plans[plan]["amount"])
    except (KeyError, TypeError, ValueError):
        return None


def _amount_matches_plan(payload: dict, plan: str) -> bool:
    """Fail closed: missing, unparsable or mismatching Amount never matches."""
    expected = _expected_plan_amount(plan)
    if expected is None:
        return False
    raw = payload.get("Amount")
    if raw is None or isinstance(raw, bool):
        return False
    try:
        return abs(float(raw) - expected) <= _AMOUNT_TOLERANCE
    except (TypeError, ValueError):
        return False


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
                    logger.warning(
                        "cloudpayments_webhook: amount does not match plan "
                        "invoice=%s account=%s plan=%s",
                        invoice_id,
                        tenant_id,
                        plan,
                    )
                    db.mark_invoice_processed(
                        invoice_id, "amount_mismatch", tenant_id
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
                    logger.warning(
                        "cloudpayments_webhook: recurrent amount does not match "
                        "plan invoice=%s account=%s plan=%s",
                        invoice_id,
                        tenant_id,
                        plan,
                    )
                    db.mark_invoice_processed(
                        invoice_id, "amount_mismatch", tenant_id
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
