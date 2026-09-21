"""Audit event store — append-only, PG-backed."""

from __future__ import annotations

import uuid
import logging
import db_adapter as db

logger = logging.getLogger("roma.audit")


def write_event(
    tenant_id: str, event_type: str, entity_type: str, entity_id: str, data: dict
) -> dict:
    """Write a single audit event. Returns {id, …}."""
    eid = str(uuid.uuid4())
    return db.insert_audit_event(
        eid, tenant_id, event_type, entity_type, entity_id, data
    )


def on_decision_allowed(
    tenant_id: str, decision_id: str, request_id: str, quota: int, cost: float
) -> dict:
    """Shortcut: write decision.allowed audit event."""
    return write_event(
        tenant_id,
        "decision.allowed",
        "decision",
        decision_id,
        {"request_id": request_id, "quota_remaining": quota, "estimated_cost": cost},
    )


def on_decision_denied(
    tenant_id: str, decision_id: str, request_id: str, reason: str
) -> dict:
    """Shortcut: write decision.denied audit event."""
    return write_event(
        tenant_id,
        "decision.denied",
        "decision",
        decision_id,
        {"request_id": request_id, "reason": reason},
    )


def on_job_created(tenant_id: str, job_id: str, decision_id: str) -> dict:
    """Shortcut: write job.created audit event."""
    return write_event(
        tenant_id,
        "job.created",
        "job",
        job_id,
        {"decision_id": decision_id},
    )
