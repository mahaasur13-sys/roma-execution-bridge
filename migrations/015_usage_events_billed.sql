-- 015_usage_events_billed.sql — RaaS-01: durable usage metering contract
--
-- usage_events lacked the `billed` column that the two canonical writers
-- (billing/pg_metering.py, db_pg_sync.py) already emit. Their INSERTs failed
-- with "column billed does not exist" and were silently swallowed into the
-- in-memory fallback, giving a false success. This adds the missing column so
-- every write path matches one schema.
--
-- billed semantics:
--   TRUE  = usage row already invoiced via a ledger DEBIT (billed)
--   FALSE = usage recorded, not yet billed
--   NULL  = legacy/unknown — rows written before this column existed; their
--           billed state cannot be recovered from a primary source, so it is
--           intentionally NOT backfilled to TRUE/FALSE.

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS billed BOOLEAN DEFAULT NULL;
