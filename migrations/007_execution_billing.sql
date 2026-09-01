-- ROMA Execution Bridge — Execution Billing Schema
-- Добавляет колонки для отслеживания стоимости выполнения

ALTER TABLE execution_jobs 
  ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS backend TEXT NOT NULL DEFAULT 'local',
  ADD COLUMN IF NOT EXISTS backend_job_id TEXT,
  ADD COLUMN IF NOT EXISTS duration_seconds DOUBLE PRECISION DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS error TEXT;
