# ROMA → DecisionOS: Enterprise Transformation Blueprint

**Date:** 2026-08-19 | **Author:** Zo Computer | **Version:** 1.0.0

---

## 0. Current State Audit

### 0.1 What ROMA Already Has (Keep & Harden)

| Layer | Module | Maturity | Notes |
|-------|--------|:--------:|-------|
| API Gateway | `main.py` (93 KB, ~1400 lines) | 🟡 | 25+ endpoints on single file. Needs decomposition. |
| Auth | `verify_api_key()` + `api_keys.json` | 🟡 | X-API-Key works. Missing rotation, JWT, token introspection. |
| Multi-Tenant | `_get_tenant_usage()`, `_check_limits()` | 🟡 | In-memory JSON files (`USAGE_FILE`). Works, not durable. |
| Decision Gate | `cost/gate.py` (207 lines) | 🟢 | `DecisionGate.check()`, quota/cost/policy triple check. Core differentiator. |
| Cost Estimator | `cost/estimator.py` (102 lines) | 🟡 | Token-based + GPU-second estimation. Needs per-model calibration. |
| Scheduler | `scheduler/roma_scheduler.py` | 🟢 | `GPUPolicyEngineV2`, tenant isolation, priority queues. |
| Workers | `worker.py`, `local_worker.py` | 🟡 | WebSocket + polling. Works. |
| Billing | `billing/cloudpayments_client.py` | 🟢 | CloudPayments webhook, `processed_invoices` table. |
| PostgreSQL Adapter | `db_pg.py` (518 lines) | 🟢 | Full async PG adapter with connection pool. Exists, NOT wired as primary. |
| Dual-DB Adapter | `db_adapter.py` (560 lines) | 🟡 | SQLite+PG router — `get_db()` returns correct adapter. Bridge exists. |
| SaaS Gateway | `saas/gateway/` | 🟢 | Rate limiter, tenant middleware, auth middleware, branding injector. |
| SaaS Pricing | `saas/pricing.py`, `saas/pricing_engine.py` | 🟡 | Tier definitions exist. Not linked to Decision Gate. |
| AI Assistant | `chat_stream()` + tool calling | 🟢 | Streaming SSE, tool execution, localStorage personalization. |
| Observability | Prometheus + Grafana dashboards | 🟢 | Metrics endpoint at `/metrics`. |
| K8s | Helm chart + Kustomize + RBAC | 🟢 | Production-ready manifests. |
| HA | `ha/` — Raft consensus, leader election | 🟡 | Implemented, not tested in production. |

### 0.2 Critical Gaps (P0 — Block Enterprise)

| # | Gap | Impact | Root Cause |
|---|-----|--------|-----------|
| 1 | **SQLite as primary store** | Jobs lost on restart, no audit trail | `db.py` (687 lines) is SQLite-only, `_save_json()` for state |
| 2 | **In-memory job store** | All jobs lost on restart | `jobs: dict = {}` in `main.py` line ~250 |
| 3 | **JSON-file state** | Usage, API keys, tenants in flat files | `USAGE_FILE`, `API_KEYS_FILE` — no concurrency, no backup |
| 4 | **No DecisionRecord entity** | Can't audit why allow/deny | Gate returns `dict`, not persisted |
| 5 | **Pricing not gated** | Gate doesn't call pricing engine | `_check_limits()` only checks quota |
| 6 | **No idempotency** | Duplicate submits create duplicate work | No idempotency key on `/submit` |
| 7 | **No retry/cancel semantics** | Jobs stuck forever | `/cancel` sets flag, no worker ack |

### 0.3 P1 Gaps

| # | Gap |
|---|-----|
| 8 | `main.py` monolith — 25 endpoints, 1400 lines, no route decomposition |
| 9 | No OpenAPI v3.1 spec for decision-centric endpoints |
| 10 | Policy engine is implicit (`_check_limits`), not pluggable |
| 11 | No per-tenant policy profiles |
| 12 | AI tool calling has no tenant-scoped authorization |
| 13 | No business metrics (decisions allowed/denied, cost-per-tenant, queue latency) |

---

## 1. Target Architecture — DecisionOS

```
                          ┌─────────────────────────────┐
                          │       API Gateway            │
                          │  X-API-Key | JWT | RBAC      │
                          └─────────────┬───────────────┘
                                        │
                   ┌────────────────────┼────────────────────┐
                   ▼                    ▼                    ▼
          ┌────────────┐      ┌────────────┐       ┌─────────────┐
          │ Decision   │      │ Execution  │       │ AI Assistant │
          │ Layer      │      │ Layer      │       │             │
          └─────┬──────┘      └─────┬──────┘       └──────┬──────┘
                │                   │                      │
                ▼                   │                      │
          ┌───────────┐             │                      │
          │ Decision  │◄────────────┘                      │
          │ Gate      │                                    │
          │ ┌───────┐ │                                    │
          │ │ Quota  │ │                                    │
          │ │ Cost   │ │                                    │
          │ │ Policy │ │                                    │
          │ └───────┘ │                                    │
          └─────┬─────┘                                    │
                │                                          │
      ┌─────────┼─────────┐                                │
      ▼         ▼         ▼                                │
  ┌──────┐ ┌──────┐ ┌──────────┐                           │
  │Allow │ │Deny  │ │Hold(for  │                           │
  │      │ │+reason│ │review)   │                           │
  └──┬───┘ └──┬───┘ └────┬─────┘                           │
     │        │          │                                  │
     ▼        ▼          ▼                                  │
┌────────────────────────────────────────────┐              │
│           Audit & Event Store              │◄─────────────┘
│  (immutable, append-only, PG-backed)       │
└────────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────────┐
│           PostgreSQL (primary)             │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐  │
│  │decisions │ │  jobs    │ │ audit_evts│  │
│  └──────────┘ └──────────┘ └───────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌───────────┐  │
│  │ tenants  │ │ billing  │ │ policies  │  │
│  └──────────┘ └──────────┘ └───────────┘  │
└────────────────────────────────────────────┘
```

### 1.1 Domain Entities

```python
# === Decision State ===
class DecisionRequest:
    id: UUID
    tenant_id: str
    user_id: str | None
    request_type: Literal["job_submit", "tool_call", "api_query", "agent_action"]
    payload: dict
    idempotency_key: str | None
    created_at: datetime

class DecisionRecord:
    id: UUID
    request_id: UUID
    gate_result: GateResult  # allowed | denied | held
    gate_reason: str         # "quota_exceeded" | "cost_over_budget" | "policy_violation" | "ok"
    quota_remaining: int
    estimated_cost: float
    policy_profile: str
    decided_at: datetime

# === Execution State ===
class ExecutionJob:
    id: UUID
    decision_id: UUID
    tenant_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    worker_id: str | None
    attempts: int
    max_retries: int
    result: dict | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

# === Economic State ===
class UsageEvent:
    id: UUID
    tenant_id: str
    job_id: UUID
    gpu_seconds: float
    tokens_in: int
    tokens_out: int
    cost: float
    recorded_at: datetime

# === Audit State ===
class AuditEvent:
    id: UUID
    tenant_id: str
    event_type: str         # "decision.allowed" | "decision.denied" | "job.started" | "job.completed" | ...
    entity_type: str        # "decision" | "job" | "billing" | "tool_call"
    entity_id: UUID
    data: dict              # full snapshot
    created_at: datetime
```

---

## 2. Data Model — PostgreSQL Schema

### 2.1 Tables

```sql
-- ===== Core =====
CREATE TABLE tenants (
    id          TEXT PRIMARY KEY,       -- e.g. "org_acme"
    name        TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'start',  -- start | pro | enterprise | white_label
    api_key_hash TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    active      BOOLEAN DEFAULT true
);

CREATE TABLE tenant_quotas (
    tenant_id       TEXT REFERENCES tenants(id),
    max_jobs_month  INT DEFAULT 50,
    max_gpu_seconds_month INT DEFAULT 36000,
    max_concurrent  INT DEFAULT 5,
    budget_limit    NUMERIC(10,2),       -- NULL = no limit
    reset_day       INT DEFAULT 1        -- day of month
);

CREATE TABLE tenant_policies (
    tenant_id       TEXT REFERENCES tenants(id),
    policy_name     TEXT NOT NULL,       -- "require_approval_over_100usd" | "block_model_gpt4" | ...
    policy_config   JSONB DEFAULT '{}',
    active          BOOLEAN DEFAULT true,
    PRIMARY KEY (tenant_id, policy_name)
);

-- ===== Decision State =====
CREATE TABLE decision_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL REFERENCES tenants(id),
    user_id         TEXT,
    request_type    TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    idempotency_key TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX idx_idempotency ON decision_requests(tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE decision_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID NOT NULL REFERENCES decision_requests(id),
    tenant_id       TEXT NOT NULL,
    gate_result     TEXT NOT NULL,       -- 'allowed' | 'denied' | 'held'
    gate_reason     TEXT NOT NULL,       -- 'ok' | 'quota_exceeded' | 'cost_over_budget' | 'policy_violation'
    quota_remaining INT,
    estimated_cost  NUMERIC(10,4),
    policy_profile  TEXT,
    decided_at      TIMESTAMPTZ DEFAULT now()
);

-- ===== Execution State =====
CREATE TABLE execution_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id     UUID NOT NULL REFERENCES decision_records(id),
    tenant_id       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    worker_id       TEXT,
    attempts        INT DEFAULT 0,
    max_retries     INT DEFAULT 3,
    payload         JSONB NOT NULL,
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX idx_jobs_tenant_status ON execution_jobs(tenant_id, status);
CREATE INDEX idx_jobs_status ON execution_jobs(status);

-- ===== Economic State =====
CREATE TABLE usage_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    job_id          UUID REFERENCES execution_jobs(id),
    gpu_seconds     NUMERIC(10,2) DEFAULT 0,
    tokens_in       INT DEFAULT 0,
    tokens_out      INT DEFAULT 0,
    cost            NUMERIC(10,4) NOT NULL,
    recorded_at     TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE billing_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    event_type      TEXT NOT NULL,       -- 'invoice_created' | 'payment_received' | 'checkout_session'
    amount          NUMERIC(10,2) NOT NULL,
    currency        TEXT DEFAULT 'USD',
    provider        TEXT,                -- 'cloudpayments' | 'stripe'
    provider_ref    TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ===== Audit State =====
CREATE TABLE audit_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       UUID NOT NULL,
    data            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_audit_tenant_ts ON audit_events(tenant_id, created_at DESC);
CREATE INDEX idx_audit_entity ON audit_events(entity_type, entity_id);
```

### 2.2 Migration Plan

| Step | Action | Risk | Downtime |
|------|--------|:----:|:--------:|
| D-1 | Create PG schema in `astrofin` database | Low | 0 |
| D-2 | Deploy `db_adapter.py` → PG router (already exists!) | Low | 0 |
| D-3 | Route `_save_json()` writes through `db_adapter` | Medium | 0 |
| D-4 | Migrate `api_keys.json` → `tenants` table | Medium | 0 |
| D-5 | Migrate `USAGE_FILE` → `usage_events` table | Low | 0 |
| D-6 | Migrate `jobs: dict` → `execution_jobs` table | High | 0 |
| D-7 | Drop SQLite path, make PG mandatory | High | brief restart |
| D-8 | Remove `roma.db`, `usage.json`, `api_keys.json` | Low | 0 |

**Total estimated:** 2-3 hours (matches your handoff estimate).

---

## 3. Enterprise Decision Gate — Design

### 3.1 Gate Pipeline

```
DecisionRequest
    │
    ▼
┌──────────────────┐
│ 1. Idempotency   │  ← Check idempotency_key → return cached result
└──────┬───────────┘
       ▼
┌──────────────────┐
│ 2. Tenant Resolve│  ← X-API-Key → tenant_id → load quota + policies
└──────┬───────────┘
       ▼
┌──────────────────┐
│ 3. Quota Check   │  ← jobs_this_month < max_jobs_month?
│                  │  ← gpu_seconds_this_month < max_gpu_seconds_month?
│                  │  ← concurrent_jobs < max_concurrent?
└──────┬───────────┘
       ▼
┌──────────────────┐
│ 4. Cost Estimate │  ← CostPredictor.estimate(request)
│                  │  ← estimated_cost < budget_remaining?
└──────┬───────────┘
       ▼
┌──────────────────┐
│ 5. Policy Engine │  ← Evaluate tenant_policies
│                  │  ← Check global policies
└──────┬───────────┘
       ▼
┌──────────────────┐
│ 6. Decision      │  → allowed → create DecisionRecord + schedule job
│                  │  → denied  → create DecisionRecord + return 402/403
│                  │  → held    → create DecisionRecord + queue for review
└──────────────────┘
```

### 3.2 New `cost/gate.py` Interface

```python
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

class GateResult(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    HELD = "held"

@dataclass
class GateDecision:
    result: GateResult
    reason: str
    quota_remaining: int | None
    estimated_cost: float | None
    policy_name: str | None
    decision_id: UUID | None

class EnterpriseDecisionGate:
    def __init__(self, db: DatabaseAdapter, cost_predictor: CostPredictor):
        ...

    async def evaluate(self, request: DecisionRequest) -> GateDecision:
        """Mandatory entry point for all execution paths."""
        # 1. Idempotency
        if request.idempotency_key:
            existing = await self._check_idempotent(request)
            if existing:
                return existing

        # 2. Tenant resolve
        tenant = await self.db.get_tenant(request.tenant_id)
        quota = await self.db.get_tenant_quota(request.tenant_id)
        policies = await self.db.get_tenant_policies(request.tenant_id)

        # 3. Quota
        quota_ok, quota_reason = await self._check_quota(tenant, quota)
        if not quota_ok:
            return GateDecision(GateResult.DENIED, quota_reason, ...)

        # 4. Cost
        cost_ok, cost_reason, estimated = await self._check_cost(request, quota)
        if not cost_ok:
            return GateDecision(GateResult.DENIED, cost_reason, ...)

        # 5. Policy
        policy_ok, policy_reason, policy_name = await self._evaluate_policies(request, policies)
        if not policy_ok:
            return GateDecision(GateResult.DENIED, policy_reason, ...)

        # 6. Record + return
        decision_id = await self._record_decision(request, GateResult.ALLOWED, "ok", ...)
        return GateDecision(GateResult.ALLOWED, "ok", ..., decision_id=decision_id)
```

### 3.3 Gate Wiring in `/submit`

```python
# BEFORE (current)
@app.post("/submit")
async def submit_task(payload: RomaTaskInput, ...):
    allowed, reason = _check_limits(tenant_id)
    if not allowed:
        raise HTTPException(402, reason)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {...}  # ← in-memory
    return RomaTaskResponse(job_id=job_id, ...)

# AFTER (DecisionOS)
@app.post("/submit")
async def submit_task(payload: RomaTaskInput, ...):
    request = DecisionRequest(
        tenant_id=key_info["tenant_id"],
        request_type="job_submit",
        payload=payload.model_dump(),
        idempotency_key=payload.idempotency_key,
    )
    decision = await gate.evaluate(request)
    if decision.result != GateResult.ALLOWED:
        raise HTTPException(402, detail=decision.reason)

    job = ExecutionJob(
        decision_id=decision.decision_id,
        tenant_id=key_info["tenant_id"],
        payload=payload.model_dump(),
    )
    await db.insert_job(job)
    return RomaTaskResponse(job_id=str(job.id), ...)
```

---

## 4. API Surface — DecisionOS

### 4.1 Preserved ROMA Endpoints (backward compatible)

| Method | Path | Status |
|--------|------|:------:|
| POST | `/submit` | Keep — wire through Gate |
| GET | `/status/{job_id}` | Keep — now PG-backed |
| GET | `/jobs` | Keep |
| POST | `/cancel/{job_id}` | Keep |
| GET | `/workers` | Keep |
| GET | `/usage` | Keep — now PG-backed |
| POST | `/billing/create-checkout-session` | Keep |
| GET | `/health` | Keep |
| GET | `/metrics` | Keep |
| GET | `/stats/daily` | Keep |
| POST | `/api/chat/stream` | Keep |

### 4.2 New DecisionOS Endpoints

| Method | Path | Description |
|--------|------|-------------|
| **Decision Layer** | | |
| POST | `/v1/decisions` | Submit decision request (no execution) |
| GET | `/v1/decisions/{id}` | Get decision record + gate result |
| GET | `/v1/decisions` | List decisions (filterable: tenant, result, date) |
| POST | `/v1/decisions/evaluate` | Dry-run: evaluate gate without executing |
| **Execution Layer** | | |
| POST | `/v1/jobs` | Submit job (decision + execution atomically) |
| GET | `/v1/jobs/{id}` | Job with full decision trail |
| POST | `/v1/jobs/{id}/retry` | Retry failed job |
| POST | `/v1/jobs/{id}/cancel` | Cancel with reason |
| **Audit Layer** | | |
| GET | `/v1/audit/events` | Query audit trail (filterable) |
| GET | `/v1/audit/decisions/{id}` | Full decision trail (request → gate → job → result) |
| **Policy Layer** | | |
| GET | `/v1/policies` | List policies for tenant |
| POST | `/v1/policies/evaluate` | Evaluate policy against hypothetical request |
| **Economic Layer** | | |
| GET | `/v1/usage/current` | Current billing period usage |
| GET | `/v1/usage/history` | Historical usage |
| GET | `/v1/billing/invoices` | Invoice history |

### 4.3 New Pydantic Models

```python
class DecisionRequestInput(BaseModel):
    request_type: Literal["job_submit", "tool_call", "agent_action"]
    payload: dict
    idempotency_key: str | None = None
    max_cost: float | None = None  # optional per-request budget cap

class DecisionResponse(BaseModel):
    decision_id: str
    result: GateResult
    reason: str
    estimated_cost: float | None
    quota_remaining: int | None
    job_id: str | None  # only if result == ALLOWED and request_type == job_submit

class JobSubmitInput(BaseModel):
    task_type: str  # "inference" | "training" | "batch" | "tool_call"
    task_config: dict
    idempotency_key: str | None = None
    priority: int = 0
    max_retries: int = 3
```

---

## 5. Migration Roadmap (30 Days)

### Week 1: Foundation (Days 1-7)

| Day | Task | Files |
|-----|------|-------|
| 1 | Create PG schema → `migrations/001_decisionos_schema.sql` | New |
| 1 | Wire `db_adapter.py` as default in `main.py` | `main.py`, `db_adapter.py` |
| 2 | Migrate `api_keys.json` → `tenants` table + seed | `main.py`, `db_adapter.py` |
| 2 | Migrate `USAGE_FILE` → `usage_events` table | `main.py` |
| 3 | Migrate `jobs: dict` → `execution_jobs` table | `main.py` |
| 4 | Add `DecisionRequest` + `DecisionRecord` models | `models/decision.py` (new) |
| 5 | Refactor `cost/gate.py` → `EnterpriseDecisionGate` | `cost/gate.py` |
| 6 | Add `AuditEvent` model + `audit/event_store.py` | `audit/` (new) |
| 7 | Drop SQLite dependency, remove `roma.db` | `db.py`, `main.py` |

### Week 2: Decision Layer (Days 8-14)

| Day | Task |
|-----|------|
| 8 | Implement `POST /v1/decisions` endpoint |
| 9 | Implement `GET /v1/decisions/{id}`, `GET /v1/decisions` |
| 10 | Implement `POST /v1/decisions/evaluate` (dry-run) |
| 11 | Wire existing `/submit` through `EnterpriseDecisionGate` |
| 12 | Add idempotency to `/submit` and `/v1/jobs` |
| 13 | Implement `/v1/jobs/{id}/retry` + retry/cancel semantics |
| 14 | Worker acknowledgment protocol (ACK on job start) |

### Week 3: Audit + Policy (Days 15-21)

| Day | Task |
|-----|------|
| 15 | Implement `GET /v1/audit/events` endpoint |
| 16 | Implement `GET /v1/audit/decisions/{id}` full trail |
| 17 | Implement policy engine (`policy/engine.py`) |
| 18 | `GET /v1/policies` + `POST /v1/policies/evaluate` |
| 19 | Wire AI tool calling through tenant-scoped Gate |
| 20 | Business metrics: `decisions_allowed/denied`, `cost_per_tenant` |
| 21 | OpenAPI v3.1 spec generation for full DecisionOS API |

### Week 4: Packaging + Hardening (Days 22-30)

| Day | Task |
|-----|------|
| 22 | Decompose `main.py` → `routers/{jobs,decisions,audit,admin}.py` |
| 23 | White-label mode: branding from `tenant_policies` |
| 24 | Load testing: 100 concurrent decisions through Gate |
| 25 | Grafana dashboard: DecisionOS business metrics |
| 26 | CI: integration tests for full decision→execution→audit flow |
| 27 | Documentation: `docs/DECISIONOS-API.md`, `docs/DECISIONOS-ARCH.md` |
| 28 | Docker Compose: `docker-compose.decisionos.yml` (PG + App + Worker) |
| 29 | Production readiness checklist audit |
| 30 | **Ship DecisionOS v1.0.0** |

---

## 6. P0/P1/P2 Change List

### P0 (Blockers — Must Complete)

| # | Change | Files | Est. |
|---|--------|-------|:----:|
| P0-1 | PG schema + migration → `migrations/001_decisionos_schema.sql` | New | 1h |
| P0-2 | Route all writes through `db_adapter` → PG | `main.py`, `db_adapter.py` | 1h |
| P0-3 | Migrate `jobs: dict` → `execution_jobs` table | `main.py` | 30m |
| P0-4 | Migrate `api_keys.json` → `tenants` table | `main.py` | 30m |
| P0-5 | Migrate `USAGE_FILE` → `usage_events` table | `main.py` | 20m |
| P0-6 | Add `DecisionRequest`, `DecisionRecord` to `EnterpriseDecisionGate` | `cost/gate.py`, `models/decision.py` | 1.5h |
| P0-7 | Wire `/submit` through Gate + idempotency | `main.py` | 1h |
| P0-8 | Add `AuditEvent` + `audit/event_store.py` | New | 1h |

### P1 (Critical — Week 2-3)

| # | Change | Files | Est. |
|---|--------|-------|:----:|
| P1-1 | DecisionOS API endpoints (`/v1/decisions/*`, `/v1/audit/*`) | `routers/decisions.py` (new) | 3h |
| P1-2 | Policy engine → `policy/engine.py` | New | 2h |
| P1-3 | Tenant-scoped AI tool calling authorization | `main.py`, `cost/gate.py` | 1h |
| P1-4 | Worker ACK + retry/cancel semantics | `worker.py`, `main.py` | 1.5h |
| P1-5 | Business metrics → Prometheus counters | `main.py`, `cost/gate.py` | 30m |
| P1-6 | `main.py` decomposition → routers | `routers/` (new) | 2h |

### P2 (Polish — Week 4)

| # | Change |
|---|--------|
| P2-1 | OpenAPI v3.1 spec (`openapi_decisionos.yaml`) |
| P2-2 | White-label branding from tenant config |
| P2-3 | Grafana business dashboard (decisions, cost, queue) |
| P2-4 | Integration tests for full audit trail |
| P2-5 | Load test: 100 concurrent Gate evaluations |
| P2-6 | `docker-compose.decisionos.yml` |

---

## 7. Production Readiness Checklist (95% Target)

| # | Criterion | Current | Target |
|---|-----------|:-------:|:------:|
| 1 | Jobs survive restart | ❌ in-memory | ✅ PG |
| 2 | Primary storage is PostgreSQL | ❌ SQLite | ✅ PG |
| 3 | Decision Gate mandatory control point | 🟡 optional | ✅ required |
| 4 | DecisionRecord persisted on every submit | ❌ none | ✅ always |
| 5 | Audit trail immutable + queryable | ❌ none | ✅ `audit_events` |
| 6 | Multi-tenant isolation | 🟡 JSON files | ✅ PG + FK |
| 7 | AI assistant tenant-scoped | ❌ unscoped | ✅ Gate on tool calls |
| 8 | Idempotency on /submit | ❌ none | ✅ idempotency_key |
| 9 | Retry/cancel semantics | 🟡 flag only | ✅ worker ack + retry loop |
| 10 | Billing linked to decision/execution flow | 🟡 partial | ✅ `billing_events` |
| 11 | Health endpoints | ✅ `/health`, `/metrics` | ✅ keep |
| 12 | Business metrics | ❌ none | ✅ decisions, cost-per-tenant, latency |
| 13 | API spec (OpenAPI v3.1) | 🟡 ROMA only | ✅ DecisionOS full |
| 14 | White-label ready | ❌ | ✅ branding from tenant config |
| 15 | Load tested (100 concurrent) | ❌ | ✅ |

---

## 8. Packaging Tiers

| Tier | ROMA Bridge | DecisionOS Start | DecisionOS Pro | DecisionOS Enterprise |
|------|:-----------:|:----------------:|:--------------:|:---------------------:|
| Price | Free/OSS | $49/mo | $89/mo | $799-$1,499/30d |
| Jobs/mo | Unlimited | 50 | 150 | Custom |
| GPU | No | No | Yes | Yes |
| Decision Gate | ❌ | ✅ | ✅ | ✅ |
| Audit Trail | ❌ | 7 days | 90 days | Forever |
| Multi-tenant | No | 1 tenant | 5 tenants | Unlimited |
| White-label | ❌ | ❌ | ❌ | ✅ |
| Policy engine | ❌ | Basic | Full | Custom |
| AI assistant | No | Read-only | Scoped tools | Full |
| SSO/SAML | ❌ | ❌ | ❌ | ✅ |
| SLA | None | 99.5% | 99.9% | 99.95% |

---

## 9. Quick Wins (Today)

These can be done NOW, before full migration:

1. **Create PG schema** — run `migrations/001_decisionos_schema.sql`
2. **Seed tenants table** — migrate `api_keys.json` → PG
3. **Add `DecisionRequest` + `DecisionRecord` models** — in `models/decision.py`
4. **Add `AuditEvent` model** — `audit/event_store.py`
5. **Wire `db_adapter.py` as primary** — flip `DB_BACKEND=postgres` default
6. **Create `start_ui.py`** — already done ✅

### Immediate Commands

```bash
# 1. Create PG schema
cd /home/workspace/roma-execution-bridge
su - postgres -c "psql -d astrofin -f migrations/001_decisionos_schema.sql"

# 2. Seed tenants from api_keys.json
python3 -c "
import json
from db_pg import get_pg_pool
keys = json.load(open('api_keys.json'))
# ... insert into tenants
"

# 3. Flip DB backend
echo 'DB_BACKEND=postgres' >> .env
```

---

## 10. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|:----------:|:------:|-----------|
| PG migration breaks existing jobs | Medium | High | Dual-write during transition, rollback SQLite |
| Gate latency > 100ms | Low | Medium | Cache quota in Redis, async policy evaluation |
| Worker ACK protocol breaks existing workers | Low | High | Backward-compatible handshake |
| `main.py` decomposition breaks routes | Medium | Medium | Decompose one router at a time, verify after each |
| Idempotency key collision | Low | Low | UUID-based keys, tenant-scoped unique index |

---

*End of DecisionOS Transformation Blueprint. Ready for implementation.*
