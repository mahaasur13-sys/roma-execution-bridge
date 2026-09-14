"""Public job routes extracted from main (A2-2)."""
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

import db_adapter as db
from deps import verify_api_key
from billing.finalize import finalize_job_billing
from billing.pg_ledger import PGUnavailableError
from models.app import RomaStatusResponse
from audit.event_store import write_event
from backends.dispatcher import backend_cancel_job

logger = logging.getLogger("roma.jobs")

router = APIRouter(tags=["jobs-public"])

@router.get("/status/{job_id}", response_model=RomaStatusResponse, dependencies=[Depends(verify_api_key)])
async def get_status(job_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    job = db.get_execution_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return RomaStatusResponse(
        job_id=job_id,
        status=job["status"],
        created_at=job["created_at"],
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        error=job.get("error"),
        backend=job.get("backend"),
        backend_job_id=job.get("backend_job_id"),
    )


@router.post("/cancel/{job_id}", dependencies=[Depends(verify_api_key)])
async def cancel_job(job_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    db.update_execution_job(job_id, status="cancelled", completed_at=datetime.now(timezone.utc).isoformat(), tenant_id=tenant_id)
    # Гасим backend-инстанс (Vast.ai destroy и т.п.)
    try:
        await backend_cancel_job(tenant_id=tenant_id, job_id=job_id)
    except Exception as exc:
        logger.warning("cancel.backend_cleanup_failed tenant=%s job=%s: %s", tenant_id, job_id, exc)
    try:
        write_event(tenant_id, "job.cancelled", "job", job_id, {})
    except Exception:
        pass
    return {"status": "cancelled", "job_id": job_id}


@router.post("/complete/{job_id}", dependencies=[Depends(verify_api_key)])
async def complete_job(job_id: str, key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")

    # Single source of truth: finalize_job_billing() debits exactly once
    # (idempotent). Actual GPU seconds are derived from the job duration.
    actual_duration_s = 0
    started_at = job.get("started_at")
    if started_at:
        try:
            started_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            actual_duration_s = (datetime.now(timezone.utc) - started_dt).total_seconds()
        except Exception:
            actual_duration_s = 0
    gpu_sec = max(actual_duration_s, 0)
    plan_name = (db.get_tenant(tenant_id) or {}).get("plan", "free")
    try:
        billing_status = finalize_job_billing(tenant_id, job_id, gpu_sec, plan_name,
                                              backend=job.get("backend"))
    except PGUnavailableError as exc:
        # fail-closed: деньги не списаны — не помечаем completed.
        logger.error("complete.billing_pg_error job=%s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail="Billing unavailable, retry later")
    if billing_status == "no_funds":
        raise HTTPException(status_code=402, detail="Insufficient funds")
    if billing_status == "skip":
        return {"status": job.get("status"), "job_id": job_id, "billing": "skip"}

    # Cleanup backend instance (Vast.ai destroy etc.)
    try:
        await backend_cancel_job(tenant_id=tenant_id, job_id=job_id)
    except Exception as exc:
        logger.warning("backend.cleanup.failed tenant=%s job=%s: %s", tenant_id, job_id, exc)

    db.update_execution_job(job_id, status="completed", completed_at=datetime.utcnow().isoformat(), tenant_id=tenant_id)
    return {"status": "completed", "job_id": job_id}


@router.get("/jobs", dependencies=[Depends(verify_api_key)])
async def list_jobs(key_info: dict = Depends(verify_api_key)):
    tenant_id = key_info["tenant_id"]
    my_jobs = db.list_jobs(tenant_id, limit=100)
    return {
        "rom_version": "1.0.0",
        "tenant_id": tenant_id,
        "queue": len(my_jobs),
        "jobs": my_jobs[-10:],
        "execution_modes": ["k8s_job", "k8s_persistent", "atom_cluster", "batch", "vastai", "local"],
    }


