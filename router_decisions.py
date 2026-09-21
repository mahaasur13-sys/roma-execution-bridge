"""DecisionOS — /v1/decisions router (Week 4 decomposited)."""

from fastapi import APIRouter, Header, HTTPException, Query
import uuid
import logging

import db_adapter as db
from cost.gate import EnterpriseDecisionGate
from policy_engine import evaluate_policies
from audit_events import write_audit_event

logger = logging.getLogger("roma.decisions")
router = APIRouter(prefix="/v1/decisions", tags=["decisions"])
gate = EnterpriseDecisionGate()


def _tenant_from_key(x_api_key: str = Header(None)) -> str:
    if not x_api_key:
        raise HTTPException(401, "Missing X-API-Key")
    tenant = db.get_tenant(x_api_key)
    if not tenant:
        tenant_id = x_api_key if len(x_api_key) > 20 else "tenant-" + x_api_key[:12]
        if not db.get_tenant(tenant_id):
            raise HTTPException(401, "Invalid API key")
        return tenant_id
    return tenant.get("id", x_api_key)


@router.post("")
async def create_decision(payload: dict, x_api_key: str = Header(None)):
    tenant_id = _tenant_from_key(x_api_key)
    request_type = payload.get("request_type", "job_submit")
    idem_key = payload.get("idempotency_key")
    _max_cost = payload.get("max_cost")
    pay = payload.get("payload", {})

    # Policy check
    pol = evaluate_policies(
        tenant_id,
        "decision.create",
        {
            "request_type": request_type,
            "estimated_cost": 0.01,
            "priority": pay.get("priority", 5),
        },
    )
    if pol["result"] != "allowed":
        write_audit_event(
            tenant_id,
            "policy.denied",
            "decision",
            str(uuid.uuid4()),
            {"reason": pol["reason"], "policy_name": pol["policy_name"]},
        )
        raise HTTPException(402, pol["reason"])

    # Idempotency
    if idem_key:
        prev = db.find_decision_by_idempotency(tenant_id, idem_key)
        if prev:
            return {
                "decision_id": prev.get("id"),
                "result": prev.get("gate_result"),
                "reason": prev.get("gate_reason"),
                "estimated_cost": prev.get("estimated_cost", 0),
                "quota_remaining": prev.get("quota_remaining", 0),
                "job_id": None,
                "idempotent": True,
            }

    # Gate
    decision = gate.evaluate(tenant_id, pay)
    did = str(uuid.uuid4())
    rid = str(uuid.uuid4())

    db.insert_decision_request(rid, tenant_id, request_type, pay, idem_key)
    db.insert_decision_record(
        did,
        rid,
        tenant_id,
        decision.result.value,
        decision.reason,
        decision.job_limit - db.count_jobs_for_tenant_total(tenant_id),
        decision.cost_estimated,
        "default",
    )

    write_audit_event(
        tenant_id,
        "decision.allowed" if decision.result.value == "allowed" else "decision.denied",
        "decision",
        did,
        {"request_id": rid, "reason": decision.reason},
    )

    job_id = None
    if decision.result.value == "allowed" and request_type == "job_submit":
        job_id = str(uuid.uuid4())
        db.insert_execution_job(job_id, did, tenant_id, "queued", pay)
        write_audit_event(tenant_id, "job.created", "job", job_id, {"decision_id": did})

    return {
        "decision_id": did,
        "result": decision.result.value,
        "reason": decision.reason,
        "estimated_cost": decision.cost_estimated,
        "quota_remaining": decision.job_limit,
        "job_id": job_id,
    }


@router.get("/{decision_id}")
async def get_decision(decision_id: str, x_api_key: str = Header(None)):
    _tenant_id = _tenant_from_key(x_api_key)
    rec = db.get_decision_record(decision_id)
    if not rec:
        raise HTTPException(404, "Decision not found")
    return {
        "decision_id": rec["id"],
        "result": rec.get("gate_result", ""),
        "reason": rec.get("gate_reason", ""),
        "estimated_cost": rec.get("estimated_cost", 0),
        "quota_remaining": rec.get("quota_remaining", 0),
        "created_at": rec.get("decided_at", ""),
    }


@router.get("")
async def list_decisions(
    result: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
    x_api_key: str = Header(None),
):
    tenant_id = _tenant_from_key(x_api_key)
    items, total = db.list_decision_records(
        tenant_id,
        result=result,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@router.post("/evaluate")
async def evaluate_decision(payload: dict, x_api_key: str = Header(None)):
    tenant_id = _tenant_from_key(x_api_key)
    pay = payload.get("payload", {})
    request_type = payload.get("request_type", "job_submit")
    pol = evaluate_policies(
        tenant_id, "decision.evaluate", {"request_type": request_type}
    )
    if pol["result"] != "allowed":
        raise HTTPException(403, pol["reason"])
    decision = gate.evaluate(tenant_id, pay)
    return {
        "result": decision.result.value,
        "reason": decision.reason,
        "estimated_cost": decision.cost_estimated,
        "quota_remaining": decision.job_limit,
        "would_create_job": decision.result.value == "allowed"
        and request_type == "job_submit",
    }
