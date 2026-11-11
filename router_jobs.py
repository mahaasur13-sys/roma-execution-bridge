"""DecisionOS — /v1/jobs router (Week 4 decomposited)."""

from fastapi import APIRouter, Header, HTTPException
import hmac
import logging
import os
from datetime import datetime, timezone

import db_adapter as db
import job_lease
from policy_engine import evaluate_policies
from policy_transition import validate_transition
from audit_events import write_audit_event
from billing.finalize import finalize_job_billing, resolve_billable_seconds
from billing.pg_ledger import PGUnavailableError
from backends.dispatcher import backend_cancel_job, get_confirmed_duration

logger = logging.getLogger("roma.jobs")
router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


def _tenant_from_key(x_api_key: str = Header(None)) -> str:
    """Resolve the tenant from the API key via the canonical DB lookup.

    Never derive a tenant_id from the key string itself.
    """
    if not x_api_key:
        raise HTTPException(401, "Missing X-API-Key")
    tenant = db.find_tenant_by_key(x_api_key)
    if not tenant:
        raise HTTPException(401, "Invalid API key")
    return tenant.get("tenant_id", "")


def _worker_token_valid(token: str | None) -> bool:
    """Timing-safe worker-token check for server-to-server calls (fail-closed)."""
    worker_token = os.getenv("ROMA_GPU_WORKER_TOKEN", "")
    if not worker_token:
        return False
    return hmac.compare_digest(token or "", worker_token)


async def _billable_seconds_or_409(job_id: str, job: dict) -> float:
    """Billable seconds (confirmed → wall-clock) or 409: unknown duration is never billed as 0."""
    confirmed = await get_confirmed_duration(job_id, job.get("backend"))
    gpu_sec = resolve_billable_seconds(confirmed, job.get("started_at"))
    if gpu_sec is None:
        logger.error("billing.duration_unknown job=%s started_at=%r", job_id, job.get("started_at"))
        raise HTTPException(409, "billing_duration_unknown: no confirmed duration and no valid started_at")
    return gpu_sec


def _lease_guard(job_id: str, job: dict, tenant_id: str, payload: dict) -> None:
    """Identity + lease до finalize (021b). Unbound — молча пропускаем."""
    ok, reason = job_lease.verdict(
        job,
        worker_id=payload.get("worker_id"),
        lease_token=payload.get("lease_token"),
        epoch=payload.get("lease_epoch"),
    )
    if ok:
        return
    data = job_lease.audit_data(job, reason)
    if job_lease.enforce_enabled():
        write_audit_event(tenant_id, "job.lease.denied", "job", job_id, data)
        raise HTTPException(409, f"lease_denied: {reason}")
    write_audit_event(tenant_id, "job.lease.shadow_mismatch", "job", job_id, data)


@router.post("/{job_id}/retry")
async def retry_job(job_id: str, payload: dict = {}, x_api_key: str = Header(None)):
    tenant_id = _tenant_from_key(x_api_key)
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        raise HTTPException(404, "Job not found")

    # Policy
    pol = evaluate_policies(tenant_id, "job.retry", {"job_status": job.get("status")})
    if pol["result"] != "allowed":
        write_audit_event(
            tenant_id,
            "policy.denied",
            "job",
            job_id,
            {"action": "retry", "reason": pol["reason"]},
        )
        raise HTTPException(403, pol["reason"])

    # Transition guard
    tr = validate_transition(job.get("status", ""), "queued")
    if not tr["allowed"]:
        write_audit_event(
            tenant_id,
            "transition.denied",
            "job",
            job_id,
            {"from": job.get("status"), "to": "queued", "reason": tr["reason"]},
        )
        raise HTTPException(409, tr["reason"])

    db.update_execution_job(job_id, status="queued", tenant_id=tenant_id)
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
        write_audit_event(
            tenant_id,
            "transition.denied",
            "job",
            job_id,
            {"from": current, "to": "cancelled"},
        )
        raise HTTPException(409, tr["reason"])

    n = db.update_execution_job(
        job_id,
        status="cancelled",
        completed_at=datetime.now(timezone.utc).isoformat(),
        tenant_id=tenant_id,
        if_status_not_in=db.CANCEL_BLOCKED_STATUSES,
    )
    if not n:
        # Статус сменился между проверкой и записью (например, running → billing_pending).
        latest = (db.get_execution_job(job_id) or job).get("status")
        raise HTTPException(409, f"transition_denied: {latest} → cancelled invalid")
    write_audit_event(tenant_id, "job.cancel", "job", job_id, {"previous_status": current})
    # Гасим backend-инстанс (Vast.ai destroy и т.п.) — как /cancel/{id}.
    try:
        await backend_cancel_job(tenant_id=tenant_id, job_id=job_id)
    except Exception as exc:
        # Отмена уже состоялась; ответ клиенту не меняем, но сбой не глотаем.
        logger.warning("cancel.backend_cleanup_failed tenant=%s job=%s: %s", tenant_id, job_id, exc)
        write_audit_event(
            tenant_id,
            "job.cancel.backend_cleanup_failed",
            "job",
            job_id,
            {"error": str(exc)},
        )
    return {"job_id": job_id, "status": "cancelled"}


@router.post("/{job_id}/complete")
async def complete_job(job_id: str, payload: dict = {}, x_api_key: str = Header(None)):
    tenant_id = _tenant_from_key(x_api_key)
    job = db.get_execution_job(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        raise HTTPException(404, "Job not found")
    current = job.get("status", "")
    tr = validate_transition(current, "completed")
    if not tr["allowed"]:
        write_audit_event(
            tenant_id,
            "transition.denied",
            "job",
            job_id,
            {"from": current, "to": "completed"},
        )
        raise HTTPException(409, tr["reason"])
    _lease_guard(job_id, job, tenant_id, payload)
    # Billing: debit exactly once before marking completed (fail-closed).
    gpu_sec = await _billable_seconds_or_409(job_id, job)
    plan_name = (db.get_tenant(tenant_id) or {}).get("plan", "free")
    try:
        billing_status = finalize_job_billing(tenant_id, job_id, gpu_sec, plan_name, backend=job.get("backend"))
    except PGUnavailableError as exc:
        logger.error("complete.billing_pg_error job=%s: %s", job_id, exc)
        raise HTTPException(503, "Billing unavailable, retry later")
    if billing_status == "no_funds":
        raise HTTPException(402, "Insufficient funds")
    if billing_status == "mismatch":
        # R2: ключ идемпотентности занят другой суммой/типом/валютой/job_id.
        raise HTTPException(409, "billing_idempotency_mismatch")
    if billing_status == "skip":
        return {"job_id": job_id, "status": job.get("status"), "billing": "skip"}
    # CAS: завершаем только если статус с момента чтения не стал терминальным
    # (cancel/свипер выиграли гонку). Дебет уже прошёл — только сигнал, без возврата.
    n = db.update_execution_job(
        job_id,
        status="completed",
        completed_at=datetime.now(timezone.utc).isoformat(),
        tenant_id=tenant_id,
        if_status_not_in=db.CANCEL_BLOCKED_STATUSES,
    )
    if not n:
        latest = (db.get_execution_job(job_id) or job).get("status")
        logger.warning("complete.cas_lost job=%s tenant=%s status=%s", job_id, tenant_id, latest)
        write_audit_event(
            tenant_id,
            "job.complete.cas_lost",
            "job",
            job_id,
            {"status": latest, "debited": True},
        )
        raise HTTPException(409, f"transition_denied: {latest} → completed invalid")
    return {"job_id": job_id, "status": "completed"}


@router.post("/{job_id}/worker-ack")
async def worker_ack(
    job_id: str,
    payload: dict,
    x_api_key: str = Header(None),
    x_worker_token: str = Header(None, alias="X-Roma-Worker-Token"),
):
    # Server-to-server (worker token) takes precedence; without it the caller
    # is tenant-scoped and may only acknowledge their own job (legacy).
    action = payload.get("action", "start")
    if action not in ("start", "complete", "fail"):
        raise HTTPException(400, "action must be start|complete|fail")
    if _worker_token_valid(x_worker_token):
        job = db.get_execution_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        tenant_id = job.get("tenant_id")
    else:
        tenant_id = _tenant_from_key(x_api_key)
        job = db.get_execution_job(job_id)
        if not job or job.get("tenant_id") != tenant_id:
            raise HTTPException(404, "Job not found")
        if action in ("start", "fail"):
            # A tenant must not drive the worker lifecycle: running → failed skips
            # finalize_job_billing, running → running steals a lease it never held.
            # Only the s2s worker reports start/fail.
            raise HTTPException(403, f"action={action} requires the worker token (X-Roma-Worker-Token)")

    status_map = {"start": "running", "complete": "completed", "fail": "failed"}
    new_status = status_map[action]
    tr = validate_transition(job.get("status", ""), new_status)
    if not tr["allowed"]:
        raise HTTPException(409, tr["reason"])

    _lease_guard(job_id, job, tenant_id, payload)

    if action == "complete":
        # Billing: debit exactly once before marking completed (fail-closed).
        gpu_sec = await _billable_seconds_or_409(job_id, job)
        plan_name = (db.get_tenant(tenant_id) or {}).get("plan", "free")
        try:
            billing_status = finalize_job_billing(tenant_id, job_id, gpu_sec, plan_name, backend=job.get("backend"))
        except PGUnavailableError as exc:
            logger.error("worker_ack.billing_pg_error job=%s: %s", job_id, exc)
            raise HTTPException(503, "Billing unavailable, retry later")
        if billing_status == "no_funds":
            raise HTTPException(402, "Insufficient funds")
        if billing_status == "mismatch":
            # R2: ключ идемпотентности занят другой суммой/типом/валютой/job_id.
            raise HTTPException(409, "billing_idempotency_mismatch")
        if billing_status == "skip":
            return {"job_id": job_id, "status": job.get("status"), "billing": "skip"}

    error = payload.get("error") if action == "fail" else None
    completed_at = datetime.now(timezone.utc).isoformat() if action in ("complete", "fail") else None
    # CAS: финальный UPDATE не перезаписывает терминальный статус, поставленный
    # параллельным cancel/свипером. Дебет (для complete) уже прошёл — только сигнал.
    n = db.update_execution_job(
        job_id,
        status=new_status,
        completed_at=completed_at,
        error=error,
        tenant_id=tenant_id,
        if_status_not_in=db.CANCEL_BLOCKED_STATUSES,
    )
    if not n:
        latest = (db.get_execution_job(job_id) or job).get("status")
        logger.warning(
            "worker_ack.cas_lost action=%s job=%s tenant=%s status=%s",
            action,
            job_id,
            tenant_id,
            latest,
        )
        write_audit_event(
            tenant_id,
            f"job.{action}.cas_lost",
            "job",
            job_id,
            {"status": latest, "debited": action == "complete", "worker_id": payload.get("worker_id")},
        )
        raise HTTPException(409, f"transition_denied: {latest} → {new_status} invalid")

    write_audit_event(
        tenant_id,
        f"job.{action}",
        "job",
        job_id,
        {"worker_id": payload.get("worker_id")},
    )
    return {"job_id": job_id, "status": new_status}
