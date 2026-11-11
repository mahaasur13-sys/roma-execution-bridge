-- 021_execution_jobs_lease.sql — 021a: только DDL аренды задачи воркером.
-- Аддитивно и идемпотентно. Без индексов, CHECK и backfill: код эти колонки
-- ещё не читает и не пишет (логика аренды — 021b отдельным GO).
-- В lease_token_hash хранится только SHA-256 токена, не сам токен.

ALTER TABLE execution_jobs ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE execution_jobs ADD COLUMN IF NOT EXISTS lease_token_hash TEXT;
ALTER TABLE execution_jobs ADD COLUMN IF NOT EXISTS lease_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE execution_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
