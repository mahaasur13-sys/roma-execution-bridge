-- 019_hosts.sql — external/own GPU host registry (phase-1 slice).
-- Additive and idempotent. No payout/money columns by design.
CREATE TABLE IF NOT EXISTS hosts (
    host_id            TEXT PRIMARY KEY,
    owner_contact_ref  TEXT NOT NULL,
    gpu_model          TEXT NOT NULL,
    gpu_count          INTEGER NOT NULL CHECK (gpu_count >= 1),
    vram_gb            NUMERIC NOT NULL CHECK (vram_gb > 0),
    region             TEXT NOT NULL DEFAULT '',
    own_host           BOOLEAN NOT NULL DEFAULT FALSE,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','verifying','active','suspended')),
    trust_tier         TEXT NOT NULL DEFAULT 'none'
                       CHECK (trust_tier IN ('none','probation','verified')),
    benchmark_score    DOUBLE PRECISION,
    last_heartbeat     TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS host_verifications (
    job_id           TEXT PRIMARY KEY,
    host_id          TEXT NOT NULL REFERENCES hosts(host_id),
    seed             TEXT NOT NULL,
    expected_sha256  TEXT NOT NULL,
    reported_sha256  TEXT,
    score            DOUBLE PRECISION,
    passed           BOOLEAN,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS host_status_events (
    id          BIGSERIAL PRIMARY KEY,
    host_id     TEXT NOT NULL REFERENCES hosts(host_id),
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE workers ADD COLUMN IF NOT EXISTS host_id TEXT;
CREATE INDEX IF NOT EXISTS hosts_status_idx ON hosts(status);
