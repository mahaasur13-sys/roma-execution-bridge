-- ROMA Execution Bridge — Idempotent job submit
-- Хранит связку (tenant_id, idempotency_key) -> job_id, чтобы повторный
-- POST /submit с тем же Idempotency-Key не создавал второй execution job.
-- PRIMARY KEY даёт UNIQUE-гарантию от гонки двух одинаковых submit.

CREATE TABLE IF NOT EXISTS submit_idempotency_keys (
    tenant_id       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, idempotency_key)
);
