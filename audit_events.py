"""DecisionOS — Audit Events (root-level, sandbox-safe)."""

import uuid
import db_adapter as db


def write_audit_event(
    tenant_id: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    data: dict | None = None,
) -> dict:
    """Write a single audit event to PG. Returns {id, ...}."""
    eid = str(uuid.uuid4())
    return db.insert_audit_event(
        eid, tenant_id, event_type, entity_type, entity_id, data or {}
    )


CRYPTO_AUDIT_EVENTS = {
    "crypto_invoice_created": "Crypto invoice created",
    "crypto_invoice_paid": "Crypto invoice payment confirmed",
    "crypto_invoice_expired": "Crypto invoice expired",
    "crypto_webhook_received": "Crypto payment webhook received",
    "crypto_webhook_verified": "Crypto payment webhook signature verified",
    "crypto_tier_activated": "Crypto payment — tier activated",
}


WALLET_AUDIT_EVENTS = {
    "crypto_wallet_created": "Crypto wallet created",
    "crypto_wallet_address_generated": "Crypto deposit address generated",
    "crypto_monero_subaddress_created": "Monero subaddress generated",
    "crypto_wallet_rotated": "Crypto wallet rotated",
}


SUPPORT_AUDIT_EVENTS = {
    "support_ticket_created": "Support ticket created",
    "support_message_sent": "Support chat message sent",
    "support_agent_assigned": "Support agent assigned to ticket",
    "support_ticket_resolved": "Support ticket resolved",
    "support_csat_submitted": "CSAT rating submitted",
}
