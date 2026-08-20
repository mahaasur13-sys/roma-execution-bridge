BEGIN;

CREATE TABLE IF NOT EXISTS ledger_entries (
    id              BIGSERIAL PRIMARY KEY,
    ledger_id       TEXT NOT NULL UNIQUE,
    tenant_id       TEXT NOT NULL,
    entry_type      TEXT NOT NULL CHECK (entry_type IN ('CREDIT', 'DEBIT')),
    amount          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    currency        TEXT NOT NULL DEFAULT 'USD',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_tenant ON ledger_entries (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_type    ON ledger_entries (entry_type, created_at DESC);

CREATE TABLE IF NOT EXISTS usage_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    value           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    job_id          TEXT DEFAULT '',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_tenant ON usage_events (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_job    ON usage_events (job_id);

CREATE TABLE IF NOT EXISTS tenant_usage_totals (
    tenant_id       TEXT PRIMARY KEY,
    gpu_seconds     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    cpu_seconds     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    gb_seconds      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    input_tokens    BIGINT NOT NULL DEFAULT 0,
    output_tokens   BIGINT NOT NULL DEFAULT 0,
    total_cost      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    jobs_completed  INTEGER NOT NULL DEFAULT 0,
    jobs_blocked    INTEGER NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;

-- Added 2026-08-20: Required for submit endpoint to work
CREATE TABLE IF NOT EXISTS execution_jobs (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT,
    tenant_id       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    payload         JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_execution_jobs_tenant ON execution_jobs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_execution_jobs_status ON execution_jobs (status);
