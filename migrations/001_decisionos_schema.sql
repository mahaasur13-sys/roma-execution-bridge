-- DecisionOS Foundation Schema
-- ROMA → DecisionOS Week 1 Migration
-- Run: su - postgres -c "psql -d astrofin -f migrations/001_decisionos_schema.sql"

BEGIN;

-- ===== Tenants =====
CREATE TABLE IF NOT EXISTS decisionos_tenants (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    tier            TEXT NOT NULL DEFAULT 'start',
    api_key_hash    TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    active          BOOLEAN DEFAULT true
);

-- ===== Tenant Quotas =====
CREATE TABLE IF NOT EXISTS decisionos_tenant_quotas (
    tenant_id            TEXT PRIMARY KEY REFERENCES decisionos_tenants(id),
    max_jobs_month       INT DEFAULT 50,
    max_gpu_seconds_month INT DEFAULT 36000,
    max_concurrent       INT DEFAULT 5,
    budget_limit         NUMERIC(10,2),
    reset_day            INT DEFAULT 1
);

-- ===== Decision Requests =====
CREATE TABLE IF NOT EXISTS decisionos_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL REFERENCES decisionos_tenants(id),
    user_id         TEXT,
    request_type    TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}',
    idempotency_key TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisionos_idempotency
    ON decisionos_requests(tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ===== Decision Records =====
CREATE TABLE IF NOT EXISTS decisionos_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID NOT NULL REFERENCES decisionos_requests(id),
    tenant_id       TEXT NOT NULL,
    gate_result     TEXT NOT NULL,
    gate_reason     TEXT NOT NULL,
    quota_remaining INT,
    estimated_cost  NUMERIC(10,4),
    decided_at      TIMESTAMPTZ DEFAULT now()
);

-- ===== Execution Jobs =====
CREATE TABLE IF NOT EXISTS decisionos_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id     UUID NOT NULL REFERENCES decisionos_records(id),
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
CREATE INDEX IF NOT EXISTS idx_decisionos_jobs_tenant_status ON decisionos_jobs(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_decisionos_jobs_status ON decisionos_jobs(status);

-- ===== Usage Events =====
CREATE TABLE IF NOT EXISTS decisionos_usage_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    job_id          UUID REFERENCES decisionos_jobs(id),
    gpu_seconds     NUMERIC(10,2) DEFAULT 0,
    tokens_in       INT DEFAULT 0,
    tokens_out      INT DEFAULT 0,
    cost            NUMERIC(10,4) NOT NULL DEFAULT 0,
    recorded_at     TIMESTAMPTZ DEFAULT now()
);

-- ===== Audit Events =====
CREATE TABLE IF NOT EXISTS decisionos_audit_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       UUID NOT NULL,
    data            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decisionos_audit_tenant_ts ON decisionos_audit_events(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisionos_audit_entity ON decisionos_audit_events(entity_type, entity_id);

COMMIT;
