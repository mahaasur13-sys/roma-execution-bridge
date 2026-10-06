-- 010_workers_capabilities.sql
-- `register_worker()` writes `capabilities` and `last_heartbeat`, but the
-- `workers` table shipped without either column — so worker registration raised
-- `UndefinedColumn` on a PG-backed deployment and no GPU worker could register.
-- Idempotent: safe to re-run on an existing cluster.
ALTER TABLE workers ADD COLUMN IF NOT EXISTS capabilities JSONB DEFAULT '{}'::jsonb;
ALTER TABLE workers ADD COLUMN IF NOT EXISTS last_heartbeat TIMESTAMPTZ;
