-- 015a_usage_events_dedup_preflight.sql — RaaS-01: preflight BEFORE the 016 unique index
--
-- Sorts before 016_usage_events_idempotency.sql, so the runner executes this
-- check BEFORE 016 attempts to create `uq_usage_events_tenant_job_type`. A live
-- `usage_events` table may already hold duplicate `(tenant_id, job_id, event_type)`
-- rows for REAL job_ids; `CREATE UNIQUE INDEX` would then fail with a raw
-- duplicate-key error that gives no count and no actionable grouping.
--
-- This preflight counts the duplicate groups under the SAME predicate as the
-- 016 index (`job_id IS NOT NULL AND job_id NOT IN ('manual','auto','')`) and
-- raises a single, actionable error with the group count when any exist.
--
-- Strictly read-only in both cases:
--   * 016 NOT yet applied — this check runs first; it never writes, never
--     creates/drops the index, and stops the runner (via RAISE) when duplicates
--     would make the 016 index impossible.
--   * 016 already applied  — the index already guarantees no duplicate groups,
--     so this check reads zero groups and changes nothing.
--
-- manual/auto/'' are intentionally NOT deduplicated (they are not unique
-- identifiers), matching 016.

DO $$
DECLARE
    duplicate_groups integer;
BEGIN
    SELECT count(*) INTO duplicate_groups
    FROM (
        SELECT 1
        FROM usage_events
        WHERE job_id IS NOT NULL AND job_id NOT IN ('manual', 'auto', '')
        GROUP BY tenant_id, job_id, event_type
        HAVING count(*) > 1
    ) d;

    IF duplicate_groups > 0 THEN
        RAISE EXCEPTION
            '015a preflight: duplicate groups = % (tenant_id, job_id, event_type) '
            'for real job_ids (same predicate as uq_usage_events_tenant_job_type); '
            'resolve before creating the unique index',
            duplicate_groups;
    END IF;
END
$$;
