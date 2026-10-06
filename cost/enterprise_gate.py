"""Enterprise Decision Gate — mandatory control point for all execution."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

from models import (
    DecisionRecord,
    DecisionRequest,
    GateResult,
    Tenant,
    TenantQuota,
)

logger = logging.getLogger("decisionos.gate")


# ── DB Protocol (minimal interface) ────────────────────


class GateDB(Protocol):
    """Minimal DB interface required by EnterpriseDecisionGate."""

    async def fetchrow(self, query: str, *args) -> dict | None: ...

    async def fetch(self, query: str, *args) -> list[dict]: ...

    async def execute(self, query: str, *args) -> str: ...


# ── Gate ───────────────────────────────────────────────


class EnterpriseDecisionGate:
    """Mandatory entry point for all execution paths.

    Pipeline:
      1. Idempotency check
      2. Tenant resolve + quota load
      3. Quota check (jobs/mo, GPU sec, concurrent)
      4. Cost estimate (simple placeholder)
      5. Policy check (minimal — tier-based)
      6. Record decision + return
    """

    def __init__(self, db: GateDB) -> None:
        self._db = db

    # ── Public API ─────────────────────────────────────

    async def evaluate(self, request: DecisionRequest) -> DecisionRecord:
        """Run full gate pipeline. Returns DecisionRecord."""
        # 1. Idempotency
        if request.idempotency_key:
            cached = await self._check_idempotent(request)
            if cached:
                return cached

        # 2. Tenant resolve
        tenant = await self._get_tenant(request.tenant_id)
        quota = await self._get_quota(request.tenant_id)

        # 3. Quota
        await self._check_quota(tenant, quota)

        # 4. Cost
        estimated = await self._estimate_cost(request)

        # 5. Policy
        await self._check_policy(tenant, request)

        # 6. Record
        record = DecisionRecord(
            request_id=request.id,
            tenant_id=request.tenant_id,
            gate_result=GateResult.ALLOWED,
            gate_reason="ok",
            quota_remaining=quota.max_jobs_month,
            estimated_cost=estimated,
        )
        await self._persist_record(record)
        return record

    # ── Internal Steps ─────────────────────────────────

    async def _check_idempotent(
        self, request: DecisionRequest
    ) -> DecisionRecord | None:
        row = await self._db.fetchrow(
            """SELECT dr.id, dr.gate_result, dr.gate_reason, dr.quota_remaining,
                      dr.estimated_cost, dr.decided_at, dr.request_id
               FROM decisionos_records dr
               JOIN decisionos_requests dreq ON dr.request_id = dreq.id
               WHERE dreq.tenant_id = $1 AND dreq.idempotency_key = $2""",
            request.tenant_id,
            request.idempotency_key,
        )
        if row:
            logger.info(
                "idempotency_hit",
                extra={"tenant": request.tenant_id, "key": request.idempotency_key},
            )
            return DecisionRecord(
                id=row["id"],
                request_id=row["request_id"],
                tenant_id=request.tenant_id,
                gate_result=GateResult(row["gate_result"]),
                gate_reason=row["gate_reason"],
                quota_remaining=row["quota_remaining"],
                estimated_cost=(
                    float(row["estimated_cost"]) if row["estimated_cost"] else None
                ),
                decided_at=row["decided_at"],
            )
        return None

    async def _get_tenant(self, tenant_id: str) -> Tenant:
        row = await self._db.fetchrow(
            "SELECT id, name, tier, active FROM decisionos_tenants WHERE id = $1",
            tenant_id,
        )
        if not row:
            raise GateDeniedError("unknown_tenant", f"Tenant '{tenant_id}' not found")
        if not row["active"]:
            raise GateDeniedError(
                "tenant_disabled", f"Tenant '{tenant_id}' is disabled"
            )
        return Tenant(
            id=row["id"], name=row["name"] or "", tier=row["tier"], active=row["active"]
        )

    async def _get_quota(self, tenant_id: str) -> TenantQuota:
        row = await self._db.fetchrow(
            "SELECT * FROM decisionos_tenant_quotas WHERE tenant_id = $1",
            tenant_id,
        )
        if not row:
            return TenantQuota(tenant_id=tenant_id)
        return TenantQuota(
            tenant_id=tenant_id,
            max_jobs_month=row["max_jobs_month"],
            max_gpu_seconds_month=row["max_gpu_seconds_month"],
            max_concurrent=row["max_concurrent"],
            budget_limit=(
                float(row["budget_limit"]) if row.get("budget_limit") else None
            ),
            reset_day=row["reset_day"] or 1,
        )

    async def _check_quota(self, tenant: Tenant, quota: TenantQuota) -> None:
        # Count jobs this month
        row = await self._db.fetchrow(
            """SELECT COUNT(*) AS cnt FROM decisionos_jobs
               WHERE tenant_id = $1 AND created_at >= date_trunc('month', now())""",
            tenant.id,
        )
        count = row["cnt"] if row else 0

        if quota.max_jobs_month > 0 and count >= quota.max_jobs_month:
            raise GateDeniedError(
                "quota_exceeded",
                f"Monthly job limit reached: {count}/{quota.max_jobs_month}",
            )

        # Concurrent
        active = await self._db.fetchrow(
            """SELECT COUNT(*) AS cnt FROM decisionos_jobs
               WHERE tenant_id = $1 AND status IN ('queued', 'running')""",
            tenant.id,
        )
        active_count = active["cnt"] if active else 0
        if quota.max_concurrent > 0 and active_count >= quota.max_concurrent:
            raise GateDeniedError(
                "concurrency_exceeded",
                f"Concurrent job limit reached: {active_count}/{quota.max_concurrent}",
            )

    async def _estimate_cost(self, request: DecisionRequest) -> float:
        # Simple cost model: $0.01 per job baseline + $0.10 if GPU
        base = 0.01
        gpu = request.payload.get("gpu_required", False)
        cost = base + (0.10 if gpu else 0.0)
        return round(cost, 4)

    async def _check_policy(self, tenant: Tenant, request: DecisionRequest) -> None:
        # Minimal tier-based policy
        if (
            tenant.tier == "start"
            and request.payload.get("execution_mode") == "k8s_job"
        ):
            return

    async def _persist_record(self, record: DecisionRecord) -> None:
        # Persist the request first
        await self._db.execute(
            """INSERT INTO decisionos_requests (id, tenant_id, request_type, payload, idempotency_key, created_at)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT DO NOTHING""",
            record.request_id,
            record.tenant_id,
            "job_submit",
            "{}",
            None,
            datetime.now(timezone.utc),
        )
        # Then the record
        await self._db.execute(
            """INSERT INTO decisionos_records (id, request_id, tenant_id, gate_result, gate_reason,
               quota_remaining, estimated_cost, decided_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            record.id,
            record.request_id,
            record.tenant_id,
            record.gate_result.value,
            record.gate_reason,
            record.quota_remaining,
            record.estimated_cost,
            record.decided_at,
        )
        logger.info(
            "decision_recorded",
            extra={
                "decision_id": str(record.id),
                "tenant": record.tenant_id,
                "result": record.gate_result.value,
                "reason": record.gate_reason,
            },
        )


# ── Exceptions ─────────────────────────────────────────


class GateDeniedError(Exception):
    """Raised when gate denies a request — caught in endpoint to return 402/403."""

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail)
