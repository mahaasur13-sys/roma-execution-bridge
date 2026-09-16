"""POST /submit — extracted from main.py:800-908 (A2). No debit at submit.

Debit happens exactly once at finalize via billing/finalize.py:44 (L2/L3 there).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request

import db_adapter as db
from deps import verify_api_key, limiter
from models.app import RomaTaskInput, RomaTaskResponse
from models.decision import DecisionRequest
from audit.event_store import on_job_created

logger = logging.getLogger("roma")
router = APIRouter(tags=["jobs-public"])


def _parse_idempotency_key(request: Request) -> Optional[str]:
    """Read and validate the Idempotency-Key header. Returns None if absent."""
    raw = request.headers.get("Idempotency-Key")
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key must not be empty")
    if len(key) > 256:
        raise HTTPException(status_code=400, detail="Idempotency-Key too long (max 256)")
    return key


def _submit_response(job_id: str, tenant_id: str, gpu_required: bool) -> RomaTaskResponse:
    return RomaTaskResponse(
        status="queued",
        job_id=job_id,
        tenant_id=tenant_id,
        roma_dispatch={"protocol": "rom", "target": f"rom://local/{job_id}"},
        dag=["validate", "dispatch", "execute", "commit"],
        estimated_resources={"cpu_cores": 2, "memory_mb": 512, "gpu": 1 if gpu_required else 0},
        gpu_required=gpu_required,
    )


@limiter.limit("30/minute")
@router.post("/submit", response_model=RomaTaskResponse, status_code=202,
             dependencies=[Depends(verify_api_key)])
async def submit_task(payload: RomaTaskInput, request: Request,
                      key_info: dict = Depends(verify_api_key)):
    import main  # lazy: queue_depth, _get_gate, metrics (avoid circular import)

    tenant_id = key_info["tenant_id"]
    idempotency_key = _parse_idempotency_key(request)

    # Replay: existing (tenant_id, Idempotency-Key) → same job.
    if idempotency_key:
        existing_job_id = db.find_job_by_idempotency(tenant_id, idempotency_key)
        if existing_job_id:
            existing = db.get_execution_job(existing_job_id)
            if existing and existing.get("tenant_id") == tenant_id:
                logger.info("submit.idempotent_replay tenant=%s key=%s job=%s",
                            tenant_id, idempotency_key[:8], existing_job_id)
                return _submit_response(
                    existing_job_id, tenant_id,
                    bool((existing.get("payload") or {}).get("gpu_required", False)),
                )

    gate = main._get_gate()

    dreq = DecisionRequest(
        tenant_id=tenant_id,
        request_type="job_submit",
        payload=payload.model_dump(),
        idempotency_key=idempotency_key,
    )

    decision = gate.evaluate(tenant_id=tenant_id, payload=payload.model_dump())
    if decision.result.value != "allowed":
        logger.warning("decision.denied tenant=%s reason=%s", tenant_id, decision.reason)
        raise HTTPException(status_code=402, detail=decision.reason)

    job_id = str(uuid.uuid4())

    # Reserve idempotency key atomically BEFORE creating the job.
    if idempotency_key:
        if not db.create_job_idempotency(tenant_id, idempotency_key, job_id):
            winner_job_id = db.find_job_by_idempotency(tenant_id, idempotency_key)
            if winner_job_id:
                existing = db.get_execution_job(winner_job_id)
                if existing and existing.get("tenant_id") == tenant_id:
                    return _submit_response(
                        winner_job_id, tenant_id,
                        bool((existing.get("payload") or {}).get("gpu_required", False)),
                    )

    main.queue_depth += 1
    main.roma_queue_depth.labels(tenant_id=tenant_id).set(main.queue_depth)

    db.insert_execution_job(
        job_id=job_id,
        decision_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        status="queued",
        payload=payload.model_dump(),
    )
    db.update_execution_job(job_id, backend=payload.backend, tenant_id=tenant_id)

    # No debit at submit: billing happens once at finalize (see /complete).
    try:
        on_job_created(tenant_id, job_id, str(uuid.uuid4()))
    except Exception as exc:
        logger.warning("audit.job_created failed tenant=%s job=%s: %s", tenant_id, job_id, exc)

    main.queue_depth -= 1
    main.roma_queue_depth.labels(tenant_id=tenant_id).set(main.queue_depth)
    main.roma_jobs_total.labels(tenant_id=tenant_id).inc()

    main.roma_jobs_active.labels(tenant_id=tenant_id).set(db.count_jobs_active_for_tenant(tenant_id))

    return _submit_response(job_id, tenant_id, payload.gpu_required)
