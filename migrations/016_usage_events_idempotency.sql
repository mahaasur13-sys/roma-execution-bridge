-- 016_usage_events_idempotency.sql — RaaS-01: idempotent usage events for real job_id
--
-- A (tenant_id, job_id, event_type) tuple identifies one real usage event, so a
-- retried write (e.g. finalize re-run) must not create a second row.
--
-- Only REAL job_ids are deduplicated. 'manual', 'auto' and '' are excluded:
-- they are not unique identifiers and remain plain (non-deduplicated) inserts.
--
-- Writes use INSERT ... ON CONFLICT ... DO NOTHING targeting this index, so a
-- repeated write is a no-op (it does NOT update cost_usd/billed of the existing
-- row).

CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_events_tenant_job_type
ON usage_events (tenant_id, job_id, event_type)
WHERE job_id IS NOT NULL AND job_id NOT IN ('manual', 'auto', '');
