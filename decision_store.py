"""Decision Store — PG-backed CRUD for DecisionOS entities.

Provides sync wrappers around db_adapter for:
  - decisions (request + record)
  - jobs (insert / read / list / status update)
  - tenants/usage
  - audit

All write paths go through PostgreSQL when PG_DSN is set.
"""

from __future__ import annotations

import uuid
import logging
import db_adapter as db

logger = logging.getLogger("roma.decision_store")


def submit_job_through_gate(tenant_id: str, request_type: str, payload: dict,
                             idempotency_key: str | None = None) -> dict:
    """Full submit flow: request → gate → decision → job.

    Returns: {decision_id, result, reason, job_id, estimated_cost, quota_remaining}
    Raises: HTTPException(402) on deny.
    """
    from fastapi import HTTPException

    request_id = str(uuid.uuid4())

    # 1. Create DecisionRequest
    db.insert_decision_request(request_id, tenant_id, request_type, payload, idempotency_key)

    # 2. Idempotency check
    if idempotency_key:
        existing = db.find_decision_by_idempotency(tenant_id, idempotency_key)
        if existing and existing.get("result") == "allowed":
            job_id = existing.get("job_id")
            return {
                "decision_id": existing["decision_id"],
                "result": "allowed",
                "reason": "idempotent_replay",
                "job_id": job_id,
                "estimated_cost": existing.get("estimated_cost", 0),
                "quota_remaining": existing.get("quota_remaining", 0),
            }

    # 3. Tenant resolve
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(status_code=401, detail=f"Tenant '{tenant_id}' not found")

    sub_status = tenant.get("subscription_status", "inactive")
    if sub_status == "inactive":
        reason = "no active subscription"
        _deny(tenant_id, request_id, reason, 0, 0)
        raise HTTPException(status_code=402, detail=reason)
    if sub_status == "past_due":
        reason = "payment past due"
        _deny(tenant_id, request_id, reason, 0, 0)
        raise HTTPException(status_code=402, detail=reason)

    plan_name = tenant.get("plan", "free")

    # 4. Quota check
    job_count = db.count_jobs_by_tenant(tenant_id)
    max_jobs = _get_plan_limit(plan_name)
    if max_jobs != -1 and job_count >= max_jobs:
        reason = f"Plan '{plan_name}' limit: {job_count}/{max_jobs}"
        _deny(tenant_id, request_id, reason, job_count, 0)
        raise HTTPException(status_code=402, detail=reason)

    # 5. Cost estimate (minimal — use fixed estimate for Week 1)
    estimated_cost = _estimate_cost(payload)

    # 6. Policy check — minimal: check subscription status
    # (Week 1: only subscription_policy)
    policy_name = "subscription_status"

    # 7. Allow
    from cost.gate import GateResult
    decision_id = str(uuid.uuid4())
    db.insert_decision_record(
        decision_id, request_id, tenant_id,
        GateResult.ALLOWED.value, "ok",
        job_count + 1, estimated_cost, policy_name,
    )

    # 8. Create job
    job_id = str(uuid.uuid4())
    job = db.insert_job(job_id, tenant_id, "queued", decision_id, payload)

    # 9. Audit
    from audit.event_store import on_decision_allowed, on_job_created
    on_decision_allowed(tenant_id, decision_id, request_id, job_count + 1, estimated_cost)
    on_job_created(tenant_id, job_id, decision_id, "job_submit")

    # 10. Record usage
    usage_id = str(uuid.uuid4())
    db.insert_usage_event(usage_id, tenant_id, job_id, 0, estimated_cost)

    return {
        "decision_id": decision_id,
        "result": "allowed",
        "reason": "ok",
        "job_id": job_id,
        "estimated_cost": estimated_cost,
        "quota_remaining": (max_jobs - job_count - 1) if max_jobs != -1 else -1,
    }


def get_job(job_id: str, tenant_id: str) -> dict | None:
    return db.get_job(job_id, tenant_id)


def list_jobs(tenant_id: str, limit: int = 100) -> list[dict]:
    return db.list_jobs(tenant_id, limit)


def cancel_job(job_id: str, tenant_id: str) -> dict | None:
    job = db.get_job(job_id, tenant_id)
    if not job:
        return None
    db.update_job_status(job_id, "cancelled")
    return db.get_job(job_id, tenant_id)


def complete_job(job_id: str, tenant_id: str) -> dict | None:
    job = db.get_job(job_id, tenant_id)
    if not job:
        return None
    import datetime
    db.update_job_status(job_id, "completed", completed_at=datetime.datetime.utcnow().isoformat())
    return db.get_job(job_id, tenant_id)


def get_tenant_usage(tenant_id: str) -> dict:
    """Return current month usage + limits."""
    tenant = db.get_tenant(tenant_id)
    plan_name = tenant.get("plan", "free") if tenant else "free"
    max_jobs = _get_plan_limit(plan_name)
    job_count = db.count_jobs_by_tenant(tenant_id)
    sub_status = tenant.get("subscription_status", "inactive") if tenant else "inactive"

    return {
        "tenant_id": tenant_id,
        "plan": plan_name,
        "subscription_status": sub_status,
        "usage": {"total_jobs": job_count, "total_gpu_seconds": 0},
        "limits": {
            "max_jobs_per_month": max_jobs,
            "max_jobs_per_month_display": "unlimited" if max_jobs == -1 else str(max_jobs),
        },
    }


def count_jobs(tenant_id: str) -> int:
    return db.count_jobs_by_tenant(tenant_id)


# ── Internals ──────────────────────────────────────────────────

def _get_plan_limit(plan_name: str) -> int:
    """Look up max_jobs_per_month from plans.json."""
    try:
        import json
        from pathlib import Path
        plans_path = Path(__file__).parent / "plans.json"
        plans = json.loads(plans_path.read_text())
        plan = plans.get(plan_name, plans.get("start", {}))
        return plan.get("max_jobs_per_month", 50)
    except Exception:
        return 50


def _estimate_cost(payload: dict) -> float:
    """Minimal cost estimate for Week 1 — 0.01 per job."""
    return 0.01


def _deny(tenant_id: str, request_id: str, reason: str,
          quota_remaining: int, estimated_cost: float):
    """Record a denied decision."""
    from cost.gate import GateResult
    import uuid
    did = str(uuid.uuid4())
    db.insert_decision_record(did, request_id, tenant_id,
                              GateResult.DENIED.value, reason,
                              quota_remaining, estimated_cost, "")
    from audit.event_store import on_decision_denied
    on_decision_denied(tenant_id, did, request_id, reason)
