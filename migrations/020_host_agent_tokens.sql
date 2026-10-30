-- 020_host_agent_tokens.sql — host agent auth + verification run details.
-- Additive and idempotent. Only SHA-256 hashes of tokens are stored. No money columns.
CREATE TABLE IF NOT EXISTS host_tokens (
    host_id       TEXT PRIMARY KEY REFERENCES hosts(host_id),
    token_sha256  TEXT NOT NULL CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS host_job_tokens (
    job_id        TEXT PRIMARY KEY REFERENCES host_verifications(job_id),
    host_id       TEXT NOT NULL REFERENCES hosts(host_id),
    token_sha256  TEXT NOT NULL CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at       TIMESTAMPTZ
);
ALTER TABLE host_verifications ADD COLUMN IF NOT EXISTS n INTEGER;
ALTER TABLE host_verifications ADD COLUMN IF NOT EXISTS reps INTEGER;
ALTER TABLE host_verifications ADD COLUMN IF NOT EXISTS min_tflops DOUBLE PRECISION;
ALTER TABLE host_verifications ADD COLUMN IF NOT EXISTS max_tflops DOUBLE PRECISION;
ALTER TABLE host_verifications ADD COLUMN IF NOT EXISTS image TEXT;
ALTER TABLE host_verifications ADD COLUMN IF NOT EXISTS elapsed_s DOUBLE PRECISION;
ALTER TABLE host_verifications ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS host_verifications_host_idx ON host_verifications(host_id);
