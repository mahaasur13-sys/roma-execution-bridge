"""ROMA SaaS — Stripe Webhook Handler + Revenue-Share."""
from fastapi import APIRouter, Request, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
import hmac
import hashlib
import time
import json
import logging
import os

router = APIRouter(prefix="/webhook", tags=["webhooks"])

STRIPE_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

logger = logging.getLogger("roma.saas.stripe_webhook")

_redis = None
try:
    import redis
    _redis = redis.from_url(REDIS_URL, decode_responses=True)
    _redis.ping()
except Exception:
    pass

def _dup(event_id: str) -> bool:
    return _redis is not None and _redis.exists(f"stripe:event:{event_id}") > 0

def _mark(event_id: str) -> None:
    if _redis:
        _redis.setex(f"stripe:event:{event_id}", 86400, str(time.time()))

def _verify(payload: bytes, sig: str, secret: str) -> bool:
    if not sig or not secret:
        return True  # Skip in dev
    try:
        parts = dict(p.split("=") for p in sig.split(","))
        signed = f"{parts['t']}.{payload.decode()}"
        actual = hmac.new(
            secret.encode(),
            signed.encode(),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(actual, parts.get("v1", ""))
    except Exception:
        return False

class Response(BaseModel):
    received: bool
    event_id: Optional[str] = None
    processed: bool = False
    error: Optional[str] = None

@router.post("/stripe", response_model=Response)
async def stripe_webhook(
    request: Request,
    x_stripe_signature: Optional[str] = Header(None),
):
    body = await request.body()

    if STRIPE_SECRET and not _verify(body, x_stripe_signature or "", STRIPE_SECRET):
        raise HTTPException(400, "Invalid signature")

    try:
        event = json.loads(body.decode())
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    eid, etype = event.get("id", ""), event.get("type", "")

    if _dup(eid):
        return Response(received=True, event_id=eid, processed=False)

    # Legacy in-process handler is disabled. The live Stripe webhook is
    # deploy/stripe-webhook/app/main.py. The ledger methods this module used
    # to call (record_usage / record_revenue_share) do not exist on
    # PGBillingLedger, so we never credit here and never report the event as
    # processed.
    logger.warning("legacy stripe_webhook handler disabled; live handler is deploy/stripe-webhook/app/main.py")
    return Response(received=True, event_id=eid, processed=False)
