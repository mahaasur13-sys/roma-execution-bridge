-- 009_ledger_append_only_trigger.sql
-- L1 (append-only ledger): ledger_entries accepts INSERT only.
-- UPDATE / DELETE / TRUNCATE raise, so a balance can never be rewritten in place —
-- corrections must be new compensating entries (CREDIT/DEBIT), which keeps the
-- audit trail complete and makes reconciliation differences meaningful.
--
-- Idempotent: safe to re-run (CREATE OR REPLACE + DROP TRIGGER IF EXISTS).
-- Expected names after applying:
--   function ledger_no_mutate()
--   trigger  ledger_entries_immutable  (BEFORE UPDATE OR DELETE, FOR EACH ROW)
--   trigger  ledger_entries_no_truncate (BEFORE TRUNCATE, FOR EACH STATEMENT)

BEGIN;

CREATE OR REPLACE FUNCTION ledger_no_mutate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'ledger_entries is append-only (L1): % is not permitted; post a compensating CREDIT/DEBIT entry instead',
        TG_OP
        USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries;
CREATE TRIGGER ledger_entries_immutable
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW
    EXECUTE FUNCTION ledger_no_mutate();

DROP TRIGGER IF EXISTS ledger_entries_no_truncate ON ledger_entries;
CREATE TRIGGER ledger_entries_no_truncate
    BEFORE TRUNCATE ON ledger_entries
    FOR EACH STATEMENT
    EXECUTE FUNCTION ledger_no_mutate();

COMMIT;
