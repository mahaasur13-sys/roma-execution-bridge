"""DecisionOS domain models — Week 1 Foundation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ──────────────────────────────────────────────


class GateResult(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    HELD = "held"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Entities ───────────────────────────────────────────


@dataclass
class Tenant:
    id: str
    name: str = ""
    tier: str = "start"
    api_key_hash: str = ""
    active: bool = True
    created_at: datetime = field(default_factory=_now)


@dataclass
class TenantQuota:
    tenant_id: str
    max_jobs_month: int = 50
    max_gpu_seconds_month: int = 36000
    max_concurrent: int = 5
    budget_limit: float | None = None
    reset_day: int = 1


@dataclass
class DecisionRequest:
    tenant_id: str
    request_type: str
    payload: dict = field(default_factory=dict)
    idempotency_key: str | None = None
    user_id: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=_now)


@dataclass
class DecisionRecord:
    request_id: UUID
    tenant_id: str
    gate_result: GateResult
    gate_reason: str
    quota_remaining: int | None = None
    estimated_cost: float | None = None
    id: UUID = field(default_factory=uuid4)
    decided_at: datetime = field(default_factory=_now)


@dataclass
class ExecutionJob:
    decision_id: UUID
    tenant_id: str
    payload: dict = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    worker_id: str | None = None
    attempts: int = 0
    max_retries: int = 3
    result: dict | None = None
    error: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class UsageEvent:
    tenant_id: str
    job_id: UUID
    cost: float = 0.0
    gpu_seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    id: UUID = field(default_factory=uuid4)
    recorded_at: datetime = field(default_factory=_now)


@dataclass
class AuditEvent:
    tenant_id: str
    event_type: str
    entity_type: str
    entity_id: UUID
    data: dict = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=_now)
