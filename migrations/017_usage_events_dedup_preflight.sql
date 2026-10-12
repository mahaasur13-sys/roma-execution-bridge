-- 017_usage_events_dedup_preflight.sql — RaaS-01: preflight for the 016 unique index
--
-- Before 016 creates `uq_usage_events_tenant_job_type`, a live `usage_events`
-- table may already hold duplicate `(tenant_id, job_id, event_type)` rows for
-- REAL job_ids; `CREATE UNIQUE INDEX` would then fail with a raw duplicate-key
-- error that gives no count and no actionable grouping.
--
-- This preflight counts the duplicate groups under the SAME predicate as the
-- 016 index (`job_id IS NOT NULL AND job_id NOT IN ('manual','auto','')`) and
-- raises a single, actionable error with the group count when any exist.
--
-- It is strictly read-only: it never DELETEs, UPDATEs, or creates/drops the
-- index. If no duplicate group exists it changes nothing.
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
            '017 preflight: duplicate groups = % (tenant_id, job_id, event_type) '
            'for real job_ids (same predicate as uq_usage_events_tenant_job_type); '
            'resolve before creating the unique index',
            duplicate_groups;
    END IF;
END
$$;
