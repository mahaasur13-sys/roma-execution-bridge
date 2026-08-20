"""DecisionOS Week 2 — Decision Service (thin wrapper around Gate + DB)."""

from __future__ import annotations

import uuid
import logging

import db_adapter as db
from cost.gate import EnterpriseDecisionGate, GateResult, estimate_cost

logger = logging.getLogger("roma.decision_service")


# ────────────────────────────────────────
#  POST /v1/decisions
# ────────────────────────────────────────
def create_decision(
    tenant_id: str,
    request_type: str,
    payload: dict,
    *,
    idempotency_key: str | None = None,
    max_cost: float = 0,
) -> dict:
    request_id = str(uuid.uuid4())
    db.insert_decision_request(request_id, tenant_id, request_type, payload, idempotency_key or "")

    gate = EnterpriseDecisionGate()
    decision = gate.evaluate(tenant_id, payload)
    quota_remaining = max(decision.job_limit - db.count_jobs_for_tenant(tenant_id), 0)
    estimated_cost_val = estimate_cost(payload)

    if decision.result == GateResult.DENIED:
        record_id = str(uuid.uuid4())
        db.insert_decision_record(
            record_id, request_id, tenant_id,
            decision.result.value, decision.reason,
            quota_remaining, estimated_cost_val,
            idempotency_key or "",
        )
        try:
            from audit.event_store import on_decision_denied
            on_decision_denied(tenant_id, record_id, request_id, decision.reason)
        except Exception:
            pass
        return {
            "decision_id": record_id,
            "result": decision.result.value,
            "reason": decision.reason,
            "estimated_cost": estimated_cost_val,
            "quota_remaining": quota_remaining,
            "job_id": None,
        }

    record_id = str(uuid.uuid4())
    db.insert_decision_record(
        record_id, request_id, tenant_id,
        decision.result.value, decision.reason,
        quota_remaining, estimated_cost_val,
        idempotency_key or "",
    )
    try:
        from audit.event_store import on_decision_allowed
        on_decision_allowed(tenant_id, record_id, request_id, decision.reason)
    except Exception:
        pass

    job_id = None
    if request_type == "job_submit":
        job_id = str(uuid.uuid4())
        db.insert_job(job_id, tenant_id, "queued", record_id, payload)
        try:
            from audit.event_store import on_job_created
            on_job_created(tenant_id, job_id, record_id)
        except Exception:
            pass

    return {
        "decision_id": record_id,
        "result": decision.result.value,
        "reason": decision.reason,
        "estimated_cost": estimated_cost_val,
        "quota_remaining": quota_remaining,
        "job_id": job_id,
    }


# ────────────────────────────────────────
#  POST /v1/decisions/evaluate  (dry-run)
# ────────────────────────────────────────
def evaluate_decision(
    tenant_id: str,
    request_type: str,
    payload: dict,
    *,
    max_cost: float = 0,
) -> dict:
    gate = EnterpriseDecisionGate()
    decision = gate.evaluate(tenant_id, payload)
    quota_remaining = max(decision.job_limit - db.count_jobs_for_tenant(tenant_id), 0)
    estimated_cost_val = estimate_cost(payload)

    return {
        "result": decision.result.value,
        "reason": decision.reason,
        "estimated_cost": estimated_cost_val,
        "quota_remaining": quota_remaining,
        "would_create_job": decision.result == GateResult.ALLOWED and request_type == "job_submit",
    }


# ────────────────────────────────────────
#  GET /v1/decisions/{id}
# ────────────────────────────────────────
def get_decision(decision_id: str, tenant_id: str) -> dict | None:
    rec = db.get_decision_record(decision_id)
    if not rec:
        return None
    if rec["tenant_id"] != tenant_id:
        return None

    # Look up linked job
    job = db.get_job_by_decision(decision_id)

    return {
        "decision_id": rec["id"],
        "request_id": rec["request_id"],
        "tenant_id": rec["tenant_id"],
        "request_type": "job_submit",
        "result": rec["gate_result"],
        "reason": rec["gate_reason"],
        "estimated_cost": rec["estimated_cost"],
        "quota_remaining": rec["quota_remaining"],
        "created_at": rec["decided_at"],
        "job": {"job_id": job["id"], "status": job["status"]} if job else None,
    }


# ────────────────────────────────────────
#  GET /v1/decisions
# ────────────────────────────────────────
def list_decisions(
    tenant_id: str,
    *,
    result: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    rows, total = db.list_decision_records(tenant_id, result, date_from, date_to, limit, offset)
    items = [
        {
            "decision_id": r["id"],
            "result": r["gate_result"],
            "reason": r["gate_reason"],
            "created_at": r["decided_at"],
            "job_id": db._get_job_id_for_decision(r["id"]),
        }
        for r in rows
    ]
    return {"items": items, "limit": limit, "offset": offset, "total": total}


# ────────────────────────────────────────
#  Idempotency helper
# ────────────────────────────────────────
def check_idempotency(tenant_id: str, idempotency_key: str) -> dict | None:
    if not idempotency_key:
        return None
    existing = db.find_decision_by_idempotency(tenant_id, idempotency_key)
    if not existing:
        return None
    job = db.get_job_by_decision(existing["id"])
    return {
        "decision_id": existing["id"],
        "result": existing["gate_result"],
        "reason": existing["gate_reason"],
        "estimated_cost": existing["estimated_cost"],
        "quota_remaining": existing["quota_remaining"],
        "job_id": job["id"] if job else None,
        "idempotent": True,
    }
