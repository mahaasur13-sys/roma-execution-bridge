"""DecisionOS — /v1/jobs router (Week 4 decomposited)."""
from fastapi import APIRouter, Header, HTTPException
import logging
from datetime import datetime, timezone

import db_adapter as db
from policy_engine import evaluate_policies
from policy_transition import validate_transition
from audit_events import write_audit_event

logger = logging.getLogger("roma.jobs")
router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


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


@router.post("/{job_id}/retry")
async def retry_job(job_id: str, payload: dict = {}, x_api_key: str = Header(None)):
    tenant_id = _tenant_from_key(x_api_key)
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        raise HTTPException(404, "Job not found")

    # Policy
    pol = evaluate_policies(tenant_id, "job.retry", {"job_status": job.get("status")})
    if pol["result"] != "allowed":
        write_audit_event(tenant_id, "policy.denied", "job", job_id,
                          {"action": "retry", "reason": pol["reason"]})
        raise HTTPException(403, pol["reason"])

    # Transition guard
    tr = validate_transition(job.get("status", ""), "queued")
    if not tr["allowed"]:
        write_audit_event(tenant_id, "transition.denied", "job", job_id,
                          {"from": job.get("status"), "to": "queued", "reason": tr["reason"]})
        raise HTTPException(409, tr["reason"])

    db.update_execution_job(job_id, status="queued")
    write_audit_event(tenant_id, "job.retry", "job", job_id, {"previous_status": job.get("status")})
    return {"job_id": job_id, "status": "queued"}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, x_api_key: str = Header(None)):
    tenant_id = _tenant_from_key(x_api_key)
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        raise HTTPException(404, "Job not found")

    pol = evaluate_policies(tenant_id, "job.cancel", {"job_status": job.get("status")})
    if pol["result"] != "allowed":
        raise HTTPException(403, pol["reason"])

    current = job.get("status", "")
    tr = validate_transition(current, "cancelled")
    if not tr["allowed"]:
        write_audit_event(tenant_id, "transition.denied", "job", job_id,
                          {"from": current, "to": "cancelled"})
        raise HTTPException(409, tr["reason"])

    db.update_execution_job(job_id, status="cancelled", completed_at=datetime.now(timezone.utc).isoformat())
    write_audit_event(tenant_id, "job.cancel", "job", job_id, {"previous_status": current})
    return {"job_id": job_id, "status": "cancelled"}


@router.post("/{job_id}/complete")
async def complete_job(job_id: str, payload: dict = {}, x_api_key: str = Header(None)):
    job = db.get_execution_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    tenant_id = job.get("tenant_id", "")
    current = job.get("status", "")
    tr = validate_transition(current, "completed")
    if not tr["allowed"]:
        write_audit_event(tenant_id, "transition.denied", "job", job_id,
                          {"from": current, "to": "completed"})
        raise HTTPException(409, tr["reason"])
    db.update_execution_job(job_id, status="completed", completed_at=datetime.now(timezone.utc).isoformat())
    return {"job_id": job_id, "status": "completed"}


@router.post("/{job_id}/worker-ack")
async def worker_ack(job_id: str, payload: dict, x_api_key: str = Header(None)):
    action = payload.get("action", "start")
    if action not in ("start", "complete", "fail"):
        raise HTTPException(400, "action must be start|complete|fail")
    job = db.get_execution_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    tenant_id = job.get("tenant_id", "")

    status_map = {"start": "running", "complete": "completed", "fail": "failed"}
    new_status = status_map[action]
    tr = validate_transition(job.get("status", ""), new_status)
    if not tr["allowed"]:
        raise HTTPException(409, tr["reason"])

    error = payload.get("error") if action == "fail" else None
    completed_at = datetime.now(timezone.utc).isoformat() if action in ("complete", "fail") else None
    db.update_execution_job(job_id, status=new_status, completed_at=completed_at, error=error)

    write_audit_event(tenant_id, f"job.{action}", "job", job_id,
                      {"worker_id": payload.get("worker_id")})
    return {"job_id": job_id, "status": new_status}
