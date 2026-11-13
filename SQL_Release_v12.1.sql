BEGIN;
ALTER TABLE public.execution_jobs
  ADD CONSTRAINT d2_execution_job_scope_uq UNIQUE (tenant_id, id);
CREATE TABLE public.d2_job_identity (
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  creation_id TEXT NOT NULL,
  store_kind TEXT NOT NULL CHECK (store_kind IN ('PG','SQLITE')),
  store_incarnation TEXT NOT NULL,
  pg_job_id TEXT,
  local_creation_receipt JSONB,
  creation_cancel_intent JSONB,
  created_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, job_id),
  UNIQUE (tenant_id, creation_id),
  CHECK (pg_job_id IS NULL OR
         (store_kind = 'PG' AND pg_job_id = job_id)),
  CHECK (local_creation_receipt IS NULL OR
         jsonb_typeof(local_creation_receipt) = 'object'),
  CHECK (creation_cancel_intent IS NULL OR
         jsonb_typeof(creation_cancel_intent) = 'object'),
  FOREIGN KEY (tenant_id, pg_job_id)
    REFERENCES public.execution_jobs(tenant_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE public.d2_financial_authority_gate (
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('OPEN','BLOCKED','CLOSED')),
  generation BIGINT NOT NULL CHECK (generation > 0),
  version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
  policy_version TEXT NOT NULL,
  revoked_at_pg TIMESTAMPTZ,
  closed_at_pg TIMESTAMPTZ,
  close_receipt JSONB,
  max_issued_receipt_expires_at_pg TIMESTAMPTZ,
  created_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, job_id),
  FOREIGN KEY (tenant_id, job_id)
    REFERENCES public.d2_job_identity(tenant_id, job_id)
    ON DELETE RESTRICT,
  CHECK ((state = 'CLOSED' AND closed_at_pg IS NOT NULL AND close_receipt IS NOT NULL)
      OR (state <> 'CLOSED' AND closed_at_pg IS NULL AND close_receipt IS NULL)),
  CHECK (close_receipt IS NULL OR jsonb_typeof(close_receipt) = 'object')
);
CREATE TABLE public.d2_attempt_store (
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  operation_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN
    ('PREPARED','BILLING_PENDING','DEBIT_COMMITTED',
     'SETTLED','COMPENSATION_PENDING','COMPENSATED',
     'INVESTIGATION_REQUIRED')),
  version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
  authority_generation BIGINT NOT NULL CHECK (authority_generation > 0),
  original_fence BIGINT NOT NULL CHECK (original_fence > 0),
  binding_id TEXT NOT NULL,
  intended_outcome JSONB NOT NULL
    CHECK (jsonb_typeof(intended_outcome) = 'object'),
  outcome_sha256 TEXT NOT NULL CHECK (outcome_sha256 ~ '^[0-9a-f]{64}$'),
  financial_snapshot JSONB NOT NULL
    CHECK (jsonb_typeof(financial_snapshot) = 'object'),
  snapshot_sha256 TEXT NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  tariff_version TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  currency TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  amount NUMERIC NOT NULL CHECK (amount >= 0 AND amount <> 'NaN'::numeric
    AND amount <> 'Infinity'::numeric),
  CHECK (amount = round(amount, 8)),
  debit_ledger_id TEXT,
  committed_debit_receipt JSONB,
  terminal_receipt_ref TEXT,
  compensation_receipt JSONB,
  incident_latch BOOLEAN NOT NULL DEFAULT FALSE,
  created_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, job_id, attempt_id),
  UNIQUE (tenant_id, job_id),
  UNIQUE (tenant_id, operation_id),
  UNIQUE (debit_ledger_id),
  FOREIGN KEY (tenant_id, job_id)
    REFERENCES public.d2_financial_authority_gate(tenant_id, job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (debit_ledger_id)
    REFERENCES public.ledger_entries(ledger_id) ON DELETE RESTRICT,
  CHECK ((debit_ledger_id IS NULL) = (committed_debit_receipt IS NULL)),
  CHECK (state NOT IN ('DEBIT_COMMITTED','SETTLED',
                       'COMPENSATION_PENDING','COMPENSATED')
         OR debit_ledger_id IS NOT NULL),
  CHECK (state <> 'SETTLED' OR terminal_receipt_ref IS NOT NULL),
  CHECK (state <> 'COMPENSATED' OR compensation_receipt IS NOT NULL
         AND jsonb_typeof(compensation_receipt)='object'),
  CHECK (NOT incident_latch OR state = 'INVESTIGATION_REQUIRED')
);
CREATE TABLE public.d2_business_admission_receipt (
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  receipt_id TEXT NOT NULL,
  command_id TEXT NOT NULL,
  attempt_id TEXT,
  request_id TEXT NOT NULL,
  writer_domain TEXT NOT NULL,
  store_incarnation TEXT NOT NULL,
  command_kind TEXT NOT NULL,
  command_sha256 TEXT NOT NULL CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  fence BIGINT NOT NULL CHECK (fence > 0),
  authority_generation BIGINT NOT NULL CHECK (authority_generation > 0),
  issued_at_pg TIMESTAMPTZ NOT NULL,
  expires_at_pg TIMESTAMPTZ NOT NULL,
  verifier_key_id TEXT NOT NULL,
  proof BYTEA NOT NULL CHECK (octet_length(proof) > 0),
  PRIMARY KEY (tenant_id, job_id, receipt_id),
  UNIQUE (tenant_id, job_id, command_id),
  UNIQUE (writer_domain, request_id),
  FOREIGN KEY (tenant_id, job_id)
    REFERENCES public.d2_financial_authority_gate(tenant_id, job_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, job_id, attempt_id)
    REFERENCES public.d2_attempt_store(tenant_id, job_id, attempt_id)
    ON DELETE RESTRICT,
  CHECK (expires_at_pg = issued_at_pg + interval '5 seconds')
);
CREATE TABLE public.d2_local_command_receipt (
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  command_id TEXT NOT NULL,
  admission_receipt_id TEXT NOT NULL,
  writer_domain TEXT NOT NULL,
  store_incarnation TEXT NOT NULL,
  command_sha256 TEXT NOT NULL CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  fence BIGINT NOT NULL CHECK (fence > 0),
  result JSONB NOT NULL,
  local_receipt_id TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  recorded_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (tenant_id, job_id, command_id),
  UNIQUE (tenant_id, job_id, admission_receipt_id),
  UNIQUE (tenant_id, job_id, local_receipt_id),
  FOREIGN KEY (tenant_id, job_id, admission_receipt_id)
    REFERENCES public.d2_business_admission_receipt(tenant_id, job_id, receipt_id)
    ON DELETE RESTRICT
);
CREATE TABLE public.d2_settlement_grant (
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  issuer_operation_id TEXT NOT NULL,
  audience TEXT NOT NULL,
  capability_kind TEXT NOT NULL CHECK (capability_kind IN
    ('SETTLEMENT','FINALIZE_ONLY_RECOVERY')),
  authority_generation BIGINT NOT NULL CHECK (authority_generation > 0),
  bound_fence BIGINT NOT NULL CHECK (bound_fence > 0),
  snapshot_sha256 TEXT NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  outcome_sha256 TEXT NOT NULL CHECK (outcome_sha256 ~ '^[0-9a-f]{64}$'),
  state TEXT NOT NULL CHECK (state IN
    ('ACTIVE','CONSUMING','CONSUMED','EXPIRED','REVOKED','INVESTIGATION_REQUIRED')),
  issued_at_pg TIMESTAMPTZ NOT NULL,
  expires_at_pg TIMESTAMPTZ NOT NULL,
  issue_receipt JSONB NOT NULL CHECK (jsonb_typeof(issue_receipt) = 'object'),
  close_receipt JSONB,
  PRIMARY KEY (tenant_id, job_id, attempt_id, grant_id),
  UNIQUE (tenant_id, issuer_operation_id),
  FOREIGN KEY (tenant_id, job_id, attempt_id)
    REFERENCES public.d2_attempt_store(tenant_id, job_id, attempt_id)
    ON DELETE RESTRICT,
  CHECK (expires_at_pg > issued_at_pg),
  CHECK ((state IN ('CONSUMED','EXPIRED','REVOKED')) =
         (close_receipt IS NOT NULL))
);
CREATE UNIQUE INDEX d2_one_active_grant
  ON public.d2_settlement_grant(tenant_id, job_id, attempt_id)
  WHERE state IN ('ACTIVE','CONSUMING');
CREATE FUNCTION public.d2_no_mutation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  RAISE EXCEPTION 'immutable evidence' USING ERRCODE = '23000';
END;
$$;
CREATE TRIGGER d2_bar_immutable
 BEFORE UPDATE OR DELETE ON public.d2_business_admission_receipt
 FOR EACH ROW EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_lcr_immutable
 BEFORE UPDATE OR DELETE ON public.d2_local_command_receipt
 FOR EACH ROW EXECUTE FUNCTION public.d2_no_mutation();
CREATE TABLE public.d2_gate_reinstate_permit (
  backend_pid INTEGER NOT NULL,
  transaction_id BIGINT NOT NULL,
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  expected_version BIGINT NOT NULL,
  reason TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
  PRIMARY KEY (backend_pid,transaction_id,tenant_id,job_id)
);
REVOKE ALL ON public.d2_gate_reinstate_permit FROM PUBLIC;
CREATE TABLE public.d2_gate_reinstatement_audit (
  reinstatement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  caller_identity TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
  requested_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  generation_before BIGINT NOT NULL CHECK (generation_before > 0),
  generation_after BIGINT NOT NULL,
  version_before BIGINT NOT NULL CHECK (version_before > 0),
  version_after BIGINT NOT NULL,
  CHECK (generation_after=generation_before+1),
  CHECK (version_after=version_before+1),
  FOREIGN KEY (tenant_id,job_id)
    REFERENCES public.d2_financial_authority_gate(tenant_id,job_id) ON DELETE RESTRICT
);
CREATE TRIGGER d2_reinstatement_audit_immutable
 BEFORE UPDATE OR DELETE ON public.d2_gate_reinstatement_audit
 FOR EACH ROW EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_reinstatement_audit_no_truncate
 BEFORE TRUNCATE ON public.d2_gate_reinstatement_audit
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE TABLE public.d2_reinstate_authorized_callers (
  caller_role TEXT PRIMARY KEY CHECK (length(btrim(caller_role)) > 0)
);
REVOKE ALL ON public.d2_gate_reinstatement_audit FROM PUBLIC;
REVOKE ALL ON public.d2_reinstate_authorized_callers FROM PUBLIC;
CREATE FUNCTION public.d2_gate_monotonic() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF NEW.policy_version IS DISTINCT FROM OLD.policy_version
     OR NEW.created_at_pg IS DISTINCT FROM OLD.created_at_pg THEN
    RAISE EXCEPTION 'gate static fields are immutable';
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state AND
     NEW.max_issued_receipt_expires_at_pg IS DISTINCT FROM OLD.max_issued_receipt_expires_at_pg THEN
    RAISE EXCEPTION 'highwater changes only through highwater exception';
  END IF;
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.job_id IS DISTINCT FROM OLD.job_id
     OR NEW.generation < OLD.generation
     OR (OLD.max_issued_receipt_expires_at_pg IS NOT NULL AND
         (NEW.max_issued_receipt_expires_at_pg IS NULL OR
          NEW.max_issued_receipt_expires_at_pg < OLD.max_issued_receipt_expires_at_pg)) THEN
    RAISE EXCEPTION 'gate regression';
  END IF;
  IF OLD.state='OPEN' AND NEW.state='OPEN'
     AND NEW.max_issued_receipt_expires_at_pg IS NOT NULL
     AND (OLD.max_issued_receipt_expires_at_pg IS NULL OR
          NEW.max_issued_receipt_expires_at_pg > OLD.max_issued_receipt_expires_at_pg)
     AND (to_jsonb(NEW)-'max_issued_receipt_expires_at_pg') IS NOT DISTINCT FROM
         (to_jsonb(OLD)-'max_issued_receipt_expires_at_pg') THEN
    RETURN NEW;
  END IF;
  IF OLD.state='CLOSED' AND NEW.state='CLOSED'
     AND NEW.generation=OLD.generation+1 AND NEW.version=OLD.version+1
     AND (to_jsonb(NEW)-ARRAY['generation','version']) IS NOT DISTINCT FROM
         (to_jsonb(OLD)-ARRAY['generation','version'])
     AND EXISTS (
       SELECT 1 FROM public.d2_job_identity_recon_pending p
         JOIN public.d2_job_identity i
           ON i.tenant_id=p.tenant_id AND i.job_id=p.job_id
         WHERE p.tenant_id=OLD.tenant_id AND p.job_id=OLD.job_id
           AND p.creation_id=i.creation_id AND i.store_kind='PG'
           AND i.pg_job_id IS NOT NULL AND i.creation_cancel_intent IS NULL
     ) THEN
    RETURN NEW;
  END IF;
  IF NOT ((OLD.state,NEW.state) IN (
    ('OPEN','BLOCKED'),('BLOCKED','OPEN'),('OPEN','CLOSED'),('BLOCKED','CLOSED')
  )) THEN RAISE EXCEPTION 'gate transition denied'; END IF;
  IF NEW.version <> OLD.version+1 THEN
    RAISE EXCEPTION 'gate version mismatch';
  END IF;
  IF NEW.state='CLOSED' THEN
    IF NEW.close_receipt IS NULL OR NEW.closed_at_pg IS NULL
       OR NEW.generation <> OLD.generation THEN
      RAISE EXCEPTION 'invalid gate closure';
    END IF;
  ELSIF NEW.generation <> OLD.generation+1 THEN
    RAISE EXCEPTION 'gate generation mismatch';
  END IF;
  IF OLD.state='OPEN' AND NEW.state='BLOCKED' AND NEW.revoked_at_pg IS NULL THEN
    RAISE EXCEPTION 'revocation timestamp required';
  END IF;
  IF OLD.state='BLOCKED' AND NEW.state='OPEN' THEN
    IF NEW.revoked_at_pg IS NOT NULL THEN RAISE EXCEPTION 'invalid reinstatement'; END IF;
    DELETE FROM public.d2_gate_reinstate_permit
      WHERE backend_pid=pg_backend_pid() AND transaction_id=txid_current()
        AND tenant_id=OLD.tenant_id AND job_id=OLD.job_id
        AND expected_version=OLD.version;
    IF NOT FOUND THEN RAISE EXCEPTION 'reinstatement requires d2_reinstate_gate'; END IF;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_gate_monotonic
 BEFORE UPDATE ON public.d2_financial_authority_gate
 FOR EACH ROW EXECUTE FUNCTION public.d2_gate_monotonic();
CREATE FUNCTION public.d2_attempt_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF (to_jsonb(NEW) - ARRAY['state','version','updated_at_pg',
       'debit_ledger_id','committed_debit_receipt',
       'terminal_receipt_ref','compensation_receipt','incident_latch'])
     IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY['state','version','updated_at_pg',
       'debit_ledger_id','committed_debit_receipt',
       'terminal_receipt_ref','compensation_receipt','incident_latch'])
     OR (OLD.debit_ledger_id IS NOT NULL AND
         ROW(NEW.debit_ledger_id,NEW.committed_debit_receipt)
         IS DISTINCT FROM
         ROW(OLD.debit_ledger_id,OLD.committed_debit_receipt))
     OR (OLD.terminal_receipt_ref IS NOT NULL AND
         NEW.terminal_receipt_ref IS DISTINCT FROM OLD.terminal_receipt_ref)
     OR (OLD.compensation_receipt IS NOT NULL AND
         NEW.compensation_receipt IS DISTINCT FROM OLD.compensation_receipt)
     OR (OLD.incident_latch AND NOT NEW.incident_latch) THEN
    RAISE EXCEPTION 'attempt evidence regression';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_attempt_immutable
 BEFORE UPDATE ON public.d2_attempt_store
 FOR EACH ROW EXECUTE FUNCTION public.d2_attempt_immutable();
CREATE FUNCTION public.d2_bar_highwater() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  gate_state TEXT;
  gate_generation BIGINT;
BEGIN
  -- Isolation contract: default read committed.
  -- Gate row locked via FOR UPDATE BEFORE pending check.
  -- Pending check MUST be a separate statement after lock (new snapshot).
  -- REPEATABLE READ is FORBIDDEN for this protocol.
  -- Lock order: Gate -> pending -> identity -> attempt. Do not reorder.
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'BAR protocol requires read committed; fail closed';
  END IF;
  SELECT state, generation INTO gate_state, gate_generation
    FROM public.d2_financial_authority_gate
    WHERE tenant_id = NEW.tenant_id AND job_id = NEW.job_id
    FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'gate missing'; END IF;
  IF gate_state = 'CLOSED' THEN
    RAISE EXCEPTION 'cannot issue receipt for CLOSED gate';
  END IF;
  IF gate_state <> 'OPEN' THEN
    RAISE EXCEPTION 'cannot issue receipt for non-OPEN gate';
  END IF;
  IF NEW.authority_generation IS DISTINCT FROM gate_generation THEN
    RAISE EXCEPTION 'BAR authority generation mismatch: bar=%, gate=%', NEW.authority_generation, gate_generation;
  END IF;
  IF EXISTS (SELECT 1 FROM public.d2_job_identity_recon_pending
             WHERE tenant_id = NEW.tenant_id AND job_id = NEW.job_id) THEN
    RAISE EXCEPTION 'cannot issue BAR: reconciliation pending for tenant=%, job=%',
      NEW.tenant_id, NEW.job_id;
  END IF;
  UPDATE public.d2_financial_authority_gate
    SET max_issued_receipt_expires_at_pg =
      GREATEST(max_issued_receipt_expires_at_pg, NEW.expires_at_pg)
    WHERE tenant_id = NEW.tenant_id AND job_id = NEW.job_id
      AND (max_issued_receipt_expires_at_pg IS NULL OR
           max_issued_receipt_expires_at_pg < NEW.expires_at_pg);
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_bar_highwater
 AFTER INSERT ON public.d2_business_admission_receipt
 FOR EACH ROW EXECUTE FUNCTION public.d2_bar_highwater();
CREATE TABLE public.d2_job_identity_recon_pending (
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  creation_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  requested_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  requested_by TEXT NOT NULL,
  PRIMARY KEY (tenant_id,job_id),
  FOREIGN KEY (tenant_id,job_id) REFERENCES public.d2_job_identity(tenant_id,job_id)
    ON DELETE RESTRICT
);
CREATE TABLE public.d2_creation_reconciliation_audit (
  reconciliation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  old_pg_job_id TEXT,
  new_pg_job_id TEXT NOT NULL,
  evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
  requested_by TEXT NOT NULL,
  completed_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  generation_before BIGINT NOT NULL,
  generation_after BIGINT NOT NULL,
  version_before BIGINT NOT NULL,
  version_after BIGINT NOT NULL,
  CHECK (generation_after = generation_before + 1),
  CHECK (version_after = version_before + 1),
  FOREIGN KEY (tenant_id,job_id) REFERENCES public.d2_job_identity(tenant_id,job_id)
    ON DELETE RESTRICT
);
REVOKE ALL ON public.d2_job_identity_recon_pending FROM PUBLIC;
REVOKE ALL ON public.d2_creation_reconciliation_audit FROM PUBLIC;
CREATE TRIGGER d2_creation_reconciliation_audit_immutable
 BEFORE UPDATE OR DELETE ON public.d2_creation_reconciliation_audit
 FOR EACH ROW EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_creation_reconciliation_audit_no_truncate
 BEFORE TRUNCATE ON public.d2_creation_reconciliation_audit
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE FUNCTION public.d2_check_creation_pair() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  t TEXT; j TEXT; ident public.d2_job_identity%ROWTYPE;
  g public.d2_financial_authority_gate%ROWTYPE; js TEXT;
BEGIN
  t := NEW.tenant_id;
  IF TG_TABLE_NAME = 'execution_jobs' THEN j := NEW.id;
  ELSE j := NEW.job_id; END IF;
  SELECT * INTO ident FROM public.d2_job_identity
    WHERE tenant_id=t AND job_id=j;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT * INTO g FROM public.d2_financial_authority_gate
    WHERE tenant_id=t AND job_id=j;
  IF NOT FOUND THEN RAISE EXCEPTION 'enrolled job requires Gate'; END IF;
  IF ident.store_kind='PG' THEN
    IF ident.pg_job_id IS NULL THEN
      IF ident.creation_cancel_intent IS NOT NULL THEN
        IF g.state <> 'CLOSED' THEN
          RAISE EXCEPTION 'PG missing job requires CLOSED cancel tombstone';
        END IF;
      ELSIF NOT EXISTS (
        SELECT 1 FROM public.d2_job_identity_recon_pending
          WHERE tenant_id=t AND job_id=j
      ) THEN
        RAISE EXCEPTION 'PG missing job requires reconciliation pending';
      END IF;
    ELSE
      SELECT status INTO js FROM public.execution_jobs
        WHERE tenant_id=t AND id=ident.pg_job_id;
      IF NOT FOUND OR upper(js)='CREATION_PENDING' THEN
        RAISE EXCEPTION 'invalid PG creation pair';
      END IF;
    END IF;
  ELSIF g.state IN ('OPEN','BLOCKED')
        AND ident.local_creation_receipt IS NULL THEN
    RAISE EXCEPTION 'SQLite linked creation evidence required';
  END IF;
  RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER d2_identity_pair
 AFTER INSERT OR UPDATE ON public.d2_job_identity
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.d2_check_creation_pair();
CREATE CONSTRAINT TRIGGER d2_gate_pair
 AFTER INSERT OR UPDATE ON public.d2_financial_authority_gate
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.d2_check_creation_pair();
CREATE CONSTRAINT TRIGGER d2_job_pair
 AFTER INSERT OR UPDATE ON public.execution_jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.d2_check_creation_pair();
ALTER TABLE public.d2_business_admission_receipt
 ADD CONSTRAINT d2_bar_command_binding UNIQUE
 (tenant_id,job_id,receipt_id,command_id,writer_domain,
  store_incarnation,command_sha256,fence);
ALTER TABLE public.d2_local_command_receipt
 ADD CONSTRAINT d2_lcr_binding_fk FOREIGN KEY
 (tenant_id,job_id,admission_receipt_id,command_id,writer_domain,
  store_incarnation,command_sha256,fence)
 REFERENCES public.d2_business_admission_receipt
 (tenant_id,job_id,receipt_id,command_id,writer_domain,
  store_incarnation,command_sha256,fence) ON DELETE RESTRICT;
ALTER TABLE public.d2_attempt_store
 ADD CONSTRAINT d2_attempt_snapshot_binding UNIQUE
 (tenant_id,job_id,attempt_id,snapshot_sha256,outcome_sha256);
ALTER TABLE public.d2_settlement_grant
 ADD CONSTRAINT d2_grant_snapshot_fk FOREIGN KEY
 (tenant_id,job_id,attempt_id,snapshot_sha256,outcome_sha256)
 REFERENCES public.d2_attempt_store
 (tenant_id,job_id,attempt_id,snapshot_sha256,outcome_sha256)
 ON DELETE RESTRICT;
CREATE FUNCTION public.d2_grant_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF (to_jsonb(NEW) - ARRAY['state','close_receipt']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['state','close_receipt']) THEN
    RAISE EXCEPTION 'immutable grant scope';
  END IF;
  IF NOT ((OLD.state, NEW.state) IN (
    ('ACTIVE','CONSUMING'),
    ('CONSUMING','CONSUMED'),
    ('ACTIVE','EXPIRED'),
    ('ACTIVE','REVOKED'),
    ('ACTIVE','INVESTIGATION_REQUIRED'),
    ('CONSUMING','INVESTIGATION_REQUIRED')
  )) THEN
    RAISE EXCEPTION 'grant transition denied: % -> %', OLD.state, NEW.state;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_grant_immutable
 BEFORE UPDATE ON public.d2_settlement_grant
 FOR EACH ROW EXECUTE FUNCTION public.d2_grant_immutable();
CREATE TRIGGER d2_grant_no_delete
 BEFORE DELETE ON public.d2_settlement_grant
 FOR EACH ROW EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_grant_no_truncate
 BEFORE TRUNCATE ON public.d2_settlement_grant
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
REVOKE TRUNCATE ON public.d2_settlement_grant FROM PUBLIC;
REVOKE UPDATE ON public.d2_attempt_store FROM PUBLIC;
CREATE FUNCTION public.d2_set_grant_state(
 p_tenant_id TEXT, p_job_id TEXT, p_attempt_id TEXT, p_grant_id TEXT,
 p_expected_state TEXT, p_next_state TEXT, p_close_receipt JSONB
) RETURNS public.d2_settlement_grant
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_settlement_grant%ROWTYPE;
BEGIN
  -- CAS by state only; version not used because Grant state is monotonic per allowlist (d2_grant_immutable)
  UPDATE public.d2_settlement_grant
    SET state=p_next_state, close_receipt=p_close_receipt
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND attempt_id=p_attempt_id AND grant_id=p_grant_id
      AND state=p_expected_state
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'grant CAS lost'; END IF;
  RETURN result;
END;
$$;
REVOKE ALL ON FUNCTION public.d2_no_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_gate_monotonic() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_attempt_immutable() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_bar_highwater() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_check_creation_pair() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_grant_immutable() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_set_grant_state(TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,JSONB) FROM PUBLIC;
CREATE TRIGGER d2_attempt_store_no_truncate
 BEFORE TRUNCATE ON public.d2_attempt_store
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_business_admission_receipt_no_truncate
 BEFORE TRUNCATE ON public.d2_business_admission_receipt
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_local_command_receipt_no_truncate
 BEFORE TRUNCATE ON public.d2_local_command_receipt
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_job_identity_no_truncate
 BEFORE TRUNCATE ON public.d2_job_identity
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_financial_authority_gate_no_truncate
 BEFORE TRUNCATE ON public.d2_financial_authority_gate
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE FUNCTION public.d2_set_attempt_state(
 p_tenant_id TEXT, p_job_id TEXT, p_attempt_id TEXT,
 p_expected_state TEXT, p_next_state TEXT, p_expected_version BIGINT
) RETURNS public.d2_attempt_store
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_attempt_store%ROWTYPE;
BEGIN
  IF p_expected_state IS NULL OR p_next_state IS NULL OR NOT (
    (p_expected_state, p_next_state) IN (
      ('PREPARED','BILLING_PENDING'),
      ('DEBIT_COMMITTED','COMPENSATION_PENDING'),
      ('PREPARED','INVESTIGATION_REQUIRED'),
      ('BILLING_PENDING','INVESTIGATION_REQUIRED'),
      ('DEBIT_COMMITTED','INVESTIGATION_REQUIRED'),
      ('COMPENSATION_PENDING','INVESTIGATION_REQUIRED'),
      ('SETTLED','INVESTIGATION_REQUIRED'),
      ('COMPENSATED','INVESTIGATION_REQUIRED')
    )
  ) THEN
    RAISE EXCEPTION 'attempt transition denied: % -> %', p_expected_state, p_next_state;
  END IF;
  UPDATE public.d2_attempt_store
    SET state=p_next_state, version=version+1,
        updated_at_pg=clock_timestamp(),
        incident_latch=(incident_latch OR p_next_state='INVESTIGATION_REQUIRED')
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND attempt_id=p_attempt_id AND state=p_expected_state
      AND version=p_expected_version AND NOT incident_latch
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'attempt CAS lost or frozen'; END IF;
  RETURN result;
END;
$$;
REVOKE ALL ON FUNCTION public.d2_set_attempt_state(TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT) FROM PUBLIC;
CREATE FUNCTION public.d2_attempt_commit_debit(
 p_tenant_id TEXT, p_job_id TEXT, p_attempt_id TEXT,
 p_expected_version BIGINT, p_debit_ledger_id TEXT,
 p_committed_debit_receipt JSONB
) RETURNS public.d2_attempt_store
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_attempt_store%ROWTYPE;
BEGIN
  IF p_debit_ledger_id IS NULL OR p_committed_debit_receipt IS NULL
     OR jsonb_typeof(p_committed_debit_receipt) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'non-NULL debit ledger ID and object receipt required';
  END IF;
  UPDATE public.d2_attempt_store
    SET state='DEBIT_COMMITTED', debit_ledger_id=p_debit_ledger_id,
        committed_debit_receipt=p_committed_debit_receipt,
        version=version+1, updated_at_pg=clock_timestamp()
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND attempt_id=p_attempt_id AND state='BILLING_PENDING'
      AND version=p_expected_version AND NOT incident_latch
      AND debit_ledger_id IS NULL AND committed_debit_receipt IS NULL
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'debit evidence CAS lost or frozen'; END IF;
  RETURN result;
END;
$$;
CREATE FUNCTION public.d2_attempt_settle(
 p_tenant_id TEXT, p_job_id TEXT, p_attempt_id TEXT,
 p_expected_version BIGINT, p_terminal_receipt_ref TEXT
) RETURNS public.d2_attempt_store
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_attempt_store%ROWTYPE;
BEGIN
  IF p_terminal_receipt_ref IS NULL THEN
    RAISE EXCEPTION 'non-NULL terminal receipt required';
  END IF;
  UPDATE public.d2_attempt_store
    SET state='SETTLED', terminal_receipt_ref=p_terminal_receipt_ref,
        version=version+1, updated_at_pg=clock_timestamp()
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND attempt_id=p_attempt_id AND state='DEBIT_COMMITTED'
      AND version=p_expected_version AND NOT incident_latch
      AND terminal_receipt_ref IS NULL
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'settlement evidence CAS lost or frozen'; END IF;
  RETURN result;
END;
$$;
REVOKE ALL ON FUNCTION public.d2_attempt_commit_debit(TEXT,TEXT,TEXT,BIGINT,TEXT,JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_attempt_settle(TEXT,TEXT,TEXT,BIGINT,TEXT) FROM PUBLIC;
CREATE FUNCTION public.d2_attempt_compensate(
 p_tenant_id TEXT, p_job_id TEXT, p_attempt_id TEXT,
 p_expected_version BIGINT, p_compensation_receipt JSONB
) RETURNS public.d2_attempt_store
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_attempt_store%ROWTYPE;

BEGIN
  IF p_compensation_receipt IS NULL
     OR jsonb_typeof(p_compensation_receipt) IS DISTINCT FROM 'object'
     OR NOT (p_compensation_receipt ?& ARRAY[
       'credit_ledger_id','amount','currency','source_debit_ledger_id'])
     OR jsonb_typeof(p_compensation_receipt->'credit_ledger_id') IS DISTINCT FROM 'string'
     OR jsonb_typeof(p_compensation_receipt->'amount') IS DISTINCT FROM 'number'
     OR jsonb_typeof(p_compensation_receipt->'currency') IS DISTINCT FROM 'string'
     OR jsonb_typeof(p_compensation_receipt->'source_debit_ledger_id') IS DISTINCT FROM 'string'
     OR length(p_compensation_receipt->>'credit_ledger_id')=0 THEN
    RAISE EXCEPTION 'complete non-NULL compensation receipt required';
  END IF;
  SELECT * INTO result FROM public.d2_attempt_store
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id AND attempt_id=p_attempt_id
    FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'attempt missing'; END IF;
  -- Amount normalization: round(numeric, 8) is the single source of truth for comparison.
  IF round((p_compensation_receipt->>'amount')::numeric,8) IS DISTINCT FROM round(result.amount,8)
     OR (p_compensation_receipt->>'currency') IS DISTINCT FROM result.currency
     OR (p_compensation_receipt->>'source_debit_ledger_id') IS DISTINCT FROM result.debit_ledger_id THEN
    RAISE EXCEPTION 'compensation receipt mismatch';
  END IF;
  IF result.state='COMPENSATED' THEN
    IF result.compensation_receipt IS NOT DISTINCT FROM p_compensation_receipt THEN
      RETURN result;
    END IF;
    RAISE EXCEPTION 'compensation replay mismatch';
  END IF;
  IF NOT (result.state='COMPENSATION_PENDING') THEN
    RAISE EXCEPTION 'attempt is not COMPENSATION_PENDING';
  END IF;
  UPDATE public.d2_attempt_store
    SET state='COMPENSATED', compensation_receipt=p_compensation_receipt,
        version=version+1, updated_at_pg=clock_timestamp()
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id AND attempt_id=p_attempt_id
      AND state='COMPENSATION_PENDING' AND version=p_expected_version
      AND NOT incident_latch AND compensation_receipt IS NULL
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'compensation CAS lost or frozen'; END IF;
  RETURN result;
END;
$$;
CREATE FUNCTION public.d2_verify_debit_ledger() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  entry RECORD;
BEGIN
  SELECT tenant_id, amount, currency, type INTO entry
    FROM public.ledger_entries WHERE ledger_id=NEW.debit_ledger_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'debit ledger missing'; END IF;
  -- Amount normalization: round(numeric, 8) is the single source of truth for comparison.
  IF entry.tenant_id IS DISTINCT FROM NEW.tenant_id
     OR round(entry.amount::numeric,8) IS DISTINCT FROM round(NEW.amount,8)
     OR entry.currency IS DISTINCT FROM NEW.currency
     OR upper(entry.type) IS DISTINCT FROM 'DEBIT' THEN
    RAISE EXCEPTION 'debit ledger binding mismatch';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_verify_debit_ledger
 AFTER UPDATE ON public.d2_attempt_store
 FOR EACH ROW WHEN (NEW.debit_ledger_id IS NOT NULL
   AND NEW.debit_ledger_id IS DISTINCT FROM OLD.debit_ledger_id)
 EXECUTE FUNCTION public.d2_verify_debit_ledger();
CREATE FUNCTION public.d2_verify_compensation_ledger() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  entry RECORD;
BEGIN
  SELECT tenant_id, amount, currency, type INTO entry
    FROM public.ledger_entries
    WHERE ledger_id=NEW.compensation_receipt->>'credit_ledger_id';
  IF NOT FOUND THEN RAISE EXCEPTION 'compensation ledger missing'; END IF;
  -- Amount normalization: round(numeric, 8) is the single source of truth for comparison.
  IF entry.tenant_id IS DISTINCT FROM NEW.tenant_id
     OR round(entry.amount::numeric,8) IS DISTINCT FROM
        round((NEW.compensation_receipt->>'amount')::numeric,8)
     OR entry.currency IS DISTINCT FROM NEW.compensation_receipt->>'currency'
     OR upper(entry.type) IS DISTINCT FROM 'CREDIT' THEN
    RAISE EXCEPTION 'compensation ledger binding mismatch';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_verify_compensation_ledger
 AFTER UPDATE ON public.d2_attempt_store
 FOR EACH ROW WHEN (NEW.compensation_receipt IS NOT NULL
   AND NEW.compensation_receipt IS DISTINCT FROM OLD.compensation_receipt)
 EXECUTE FUNCTION public.d2_verify_compensation_ledger();
REVOKE ALL ON FUNCTION public.d2_attempt_compensate(TEXT,TEXT,TEXT,BIGINT,JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_verify_debit_ledger() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_verify_compensation_ledger() FROM PUBLIC;
CREATE UNIQUE INDEX d2_attempt_credit_ledger_global_uq
  ON public.d2_attempt_store ((compensation_receipt->>'credit_ledger_id'))
  WHERE compensation_receipt IS NOT NULL
    AND compensation_receipt->>'credit_ledger_id' IS NOT NULL;
CREATE FUNCTION public.d2_attempt_prepare_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF NEW.state <> 'PREPARED'
     OR NEW.debit_ledger_id IS NOT NULL
     OR NEW.committed_debit_receipt IS NOT NULL
     OR NEW.terminal_receipt_ref IS NOT NULL
     OR NEW.compensation_receipt IS NOT NULL
     OR NEW.incident_latch = TRUE THEN
    RAISE EXCEPTION 'new attempt must be PREPARED with empty evidence';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_attempt_prepare_guard
 BEFORE INSERT ON public.d2_attempt_store
 FOR EACH ROW EXECUTE FUNCTION public.d2_attempt_prepare_guard();
REVOKE ALL ON FUNCTION public.d2_attempt_prepare_guard() FROM PUBLIC;
CREATE FUNCTION public.d2_set_gate_state(
 p_tenant_id TEXT, p_job_id TEXT, p_expected_state TEXT, p_next_state TEXT,
 p_expected_version BIGINT, p_close_receipt JSONB DEFAULT NULL
) RETURNS public.d2_financial_authority_gate
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_financial_authority_gate%ROWTYPE;
BEGIN
  IF p_expected_state IS NULL OR p_next_state IS NULL OR NOT (
    (p_expected_state,p_next_state) IN (
      ('OPEN','BLOCKED'),('BLOCKED','OPEN'),('OPEN','CLOSED'),('BLOCKED','CLOSED')
    )
  ) THEN RAISE EXCEPTION 'gate transition denied'; END IF;
  IF p_expected_state='BLOCKED' AND p_next_state='OPEN' THEN
    RAISE EXCEPTION 'reinstatement requires d2_reinstate_gate';
  END IF;
  IF p_next_state='CLOSED' AND p_close_receipt IS NULL THEN
    RAISE EXCEPTION 'close receipt required';
  END IF;
  IF p_next_state<>'CLOSED' AND p_close_receipt IS NOT NULL THEN
    RAISE EXCEPTION 'unexpected close receipt';
  END IF;
  UPDATE public.d2_financial_authority_gate
    SET state=p_next_state, version=version+1,
        generation=generation+CASE WHEN p_next_state='BLOCKED' THEN 1 ELSE 0 END,
        revoked_at_pg=CASE WHEN p_next_state='BLOCKED' THEN clock_timestamp() ELSE revoked_at_pg END,
        closed_at_pg=CASE WHEN p_next_state='CLOSED' THEN clock_timestamp() ELSE NULL END,
        close_receipt=p_close_receipt
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND state=p_expected_state AND version=p_expected_version
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'gate CAS lost'; END IF;
  RETURN result;
END;
$$;
CREATE FUNCTION public.d2_reinstate_gate(
 p_tenant_id TEXT, p_job_id TEXT, p_expected_version BIGINT, p_reason TEXT
) RETURNS public.d2_financial_authority_gate
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_caller TEXT := session_user;
  v_authorized BOOLEAN := FALSE;
  result public.d2_financial_authority_gate%ROWTYPE;
BEGIN
  -- Lock order: Gate -> pending -> identity -> attempt. Do not reorder.
  SELECT TRUE INTO v_authorized FROM public.d2_reinstate_authorized_callers
    WHERE caller_role = v_caller LIMIT 1;
  IF NOT COALESCE(v_authorized, FALSE) THEN
    RAISE EXCEPTION 'unauthorized reinstatement caller: %', v_caller;
  END IF;
  IF p_reason IS NULL OR length(btrim(p_reason))=0 THEN
    RAISE EXCEPTION 'reinstatement reason required';
  END IF;
  SELECT * INTO result FROM public.d2_financial_authority_gate
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND state='BLOCKED' AND version=p_expected_version
    FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'reinstatement CAS lost'; END IF;
  INSERT INTO public.d2_gate_reinstatement_audit
    (tenant_id,job_id,caller_identity,reason,generation_before,generation_after,version_before,version_after)
    VALUES (p_tenant_id,p_job_id,v_caller,p_reason,result.generation,result.generation+1,
            result.version,result.version+1);
  INSERT INTO public.d2_gate_reinstate_permit
    (backend_pid,transaction_id,tenant_id,job_id,expected_version,reason)
    VALUES (pg_backend_pid(),txid_current(),p_tenant_id,p_job_id,p_expected_version,p_reason);
  UPDATE public.d2_financial_authority_gate
    SET state='OPEN', generation=generation+1, version=version+1,
        revoked_at_pg=NULL
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND state='BLOCKED' AND version=p_expected_version
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'reinstatement CAS lost'; END IF;
  RETURN result;
END;
$$;
CREATE FUNCTION public.d2_creation_cancel_binding() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF jsonb_typeof(NEW.creation_cancel_intent) IS DISTINCT FROM 'object'
     OR NOT (NEW.creation_cancel_intent ?& ARRAY['tenant_id','job_id','creation_id','intent_id'])
     OR (NEW.creation_cancel_intent->>'tenant_id') IS DISTINCT FROM NEW.tenant_id
     OR (NEW.creation_cancel_intent->>'job_id') IS DISTINCT FROM NEW.job_id
     OR (NEW.creation_cancel_intent->>'creation_id') IS DISTINCT FROM NEW.creation_id
     OR jsonb_typeof(NEW.creation_cancel_intent->'intent_id') IS DISTINCT FROM 'string'
     OR COALESCE(NEW.creation_cancel_intent->>'intent_id','') !~
        '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' THEN
    RAISE EXCEPTION 'creation_cancel_intent binding mismatch';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_creation_cancel_binding
 BEFORE INSERT OR UPDATE ON public.d2_job_identity
 FOR EACH ROW WHEN (NEW.creation_cancel_intent IS NOT NULL)
 EXECUTE FUNCTION public.d2_creation_cancel_binding();
REVOKE ALL ON FUNCTION public.d2_set_gate_state(TEXT,TEXT,TEXT,TEXT,BIGINT,JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_reinstate_gate(TEXT,TEXT,BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_creation_cancel_binding() FROM PUBLIC;
CREATE FUNCTION public.d2_job_identity_immutable() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF ROW(NEW.tenant_id,NEW.job_id,NEW.creation_id,NEW.store_kind,
         NEW.store_incarnation,NEW.local_creation_receipt)
     IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.job_id,OLD.creation_id,OLD.store_kind,
         OLD.store_incarnation,OLD.local_creation_receipt)
     OR (OLD.creation_cancel_intent IS NOT NULL AND
         NEW.creation_cancel_intent IS DISTINCT FROM OLD.creation_cancel_intent) THEN
    RAISE EXCEPTION 'job identity or creation evidence is immutable';
  END IF;
  IF OLD.pg_job_id IS NULL AND NEW.pg_job_id IS NOT NULL
     AND OLD.creation_cancel_intent IS NULL THEN
    IF NEW.creation_cancel_intent IS NOT NULL THEN
      RAISE EXCEPTION 'cannot combine reconciliation with creation cancellation';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM public.d2_job_identity_recon_pending
        WHERE tenant_id=OLD.tenant_id AND job_id=OLD.job_id
    ) THEN
      RAISE EXCEPTION 'pg_job_id reconciliation requires pending';
    END IF;
  ELSIF NEW.pg_job_id IS DISTINCT FROM OLD.pg_job_id THEN
    RAISE EXCEPTION 'pg_job_id is immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_job_identity_immutable
 BEFORE UPDATE ON public.d2_job_identity
 FOR EACH ROW EXECUTE FUNCTION public.d2_job_identity_immutable();
REVOKE ALL ON FUNCTION public.d2_job_identity_immutable() FROM PUBLIC;
-- SECURITY: INSERT into permit tables is protected exclusively by ACL (no direct INSERT for runtime roles). Triggers prevent altering/deleting foreign permits.
CREATE TABLE public.d2_recon_pending_insert_permit (
  backend_pid INTEGER NOT NULL,
  transaction_id BIGINT NOT NULL,
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  PRIMARY KEY (backend_pid,transaction_id,tenant_id,job_id)
);
CREATE TABLE public.d2_recon_pending_consume_permit (
  backend_pid INTEGER NOT NULL,
  transaction_id BIGINT NOT NULL,
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('RECONCILE','CANCEL')),
  PRIMARY KEY (backend_pid,transaction_id,tenant_id,job_id)
);
REVOKE ALL ON public.d2_recon_pending_insert_permit FROM PUBLIC;
REVOKE ALL ON public.d2_recon_pending_consume_permit FROM PUBLIC;
CREATE FUNCTION public.d2_recon_permit_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'reconciliation permit is immutable';
  ELSIF TG_OP = 'DELETE' THEN
    IF OLD.backend_pid IS DISTINCT FROM pg_backend_pid()
       OR OLD.transaction_id IS DISTINCT FROM txid_current() THEN
      RAISE EXCEPTION 'reconciliation permit belongs to another transaction';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.backend_pid IS DISTINCT FROM pg_backend_pid()
     OR NEW.transaction_id IS DISTINCT FROM txid_current() THEN
    RAISE EXCEPTION 'reconciliation permit must belong to current transaction';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER d2_recon_pending_insert_permit_guard
 BEFORE INSERT OR UPDATE OR DELETE ON public.d2_recon_pending_insert_permit
 FOR EACH ROW EXECUTE FUNCTION public.d2_recon_permit_guard();
CREATE TRIGGER d2_recon_pending_consume_permit_guard
 BEFORE INSERT OR UPDATE OR DELETE ON public.d2_recon_pending_consume_permit
 FOR EACH ROW EXECUTE FUNCTION public.d2_recon_permit_guard();
CREATE TRIGGER d2_recon_pending_insert_permit_no_truncate
 BEFORE TRUNCATE ON public.d2_recon_pending_insert_permit
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_recon_pending_consume_permit_no_truncate
 BEFORE TRUNCATE ON public.d2_recon_pending_consume_permit
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
REVOKE ALL ON FUNCTION public.d2_recon_permit_guard() FROM PUBLIC;
CREATE TABLE public.d2_reconciliation_cancel_audit (
  cancellation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  creation_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  requested_at_pg TIMESTAMPTZ NOT NULL,
  cancelled_by TEXT NOT NULL,
  cancelled_at_pg TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (tenant_id,job_id) REFERENCES public.d2_job_identity(tenant_id,job_id)
    ON DELETE RESTRICT
);
CREATE TRIGGER d2_reconciliation_cancel_audit_immutable
 BEFORE UPDATE OR DELETE ON public.d2_reconciliation_cancel_audit
 FOR EACH ROW EXECUTE FUNCTION public.d2_no_mutation();
CREATE TRIGGER d2_reconciliation_cancel_audit_no_truncate
 BEFORE TRUNCATE ON public.d2_reconciliation_cancel_audit
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
REVOKE ALL ON public.d2_reconciliation_cancel_audit FROM PUBLIC;
CREATE FUNCTION public.d2_recon_pending_insert_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  DELETE FROM public.d2_recon_pending_insert_permit
    WHERE backend_pid=pg_backend_pid() AND transaction_id=txid_current()
      AND tenant_id=NEW.tenant_id AND job_id=NEW.job_id
      AND requested_by=NEW.requested_by;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'recon pending insert requires d2_request_reconciliation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE FUNCTION public.d2_recon_pending_update_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF ROW(NEW.tenant_id,NEW.job_id,NEW.creation_id,NEW.requested_at_pg,NEW.requested_by)
     IS DISTINCT FROM
     ROW(OLD.tenant_id,OLD.job_id,OLD.creation_id,OLD.requested_at_pg,OLD.requested_by)
     OR NEW.reason IS DISTINCT FROM OLD.reason THEN
    RAISE EXCEPTION 'recon pending identity is immutable';
  END IF;
  RETURN NEW;
END;
$$;
CREATE FUNCTION public.d2_recon_pending_delete_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  DELETE FROM public.d2_recon_pending_consume_permit
    WHERE backend_pid=pg_backend_pid() AND transaction_id=txid_current()
      AND tenant_id=OLD.tenant_id AND job_id=OLD.job_id
      AND action IN ('RECONCILE','CANCEL');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'recon pending delete requires reconcile or cancel';
  END IF;
  RETURN OLD;
END;
$$;
CREATE TRIGGER d2_recon_pending_insert_guard
 BEFORE INSERT ON public.d2_job_identity_recon_pending
 FOR EACH ROW EXECUTE FUNCTION public.d2_recon_pending_insert_guard();
CREATE TRIGGER d2_recon_pending_update_guard
 BEFORE UPDATE ON public.d2_job_identity_recon_pending
 FOR EACH ROW EXECUTE FUNCTION public.d2_recon_pending_update_guard();
CREATE TRIGGER d2_recon_pending_delete_guard
 BEFORE DELETE ON public.d2_job_identity_recon_pending
 FOR EACH ROW EXECUTE FUNCTION public.d2_recon_pending_delete_guard();
CREATE TRIGGER d2_recon_pending_no_truncate
 BEFORE TRUNCATE ON public.d2_job_identity_recon_pending
 FOR EACH STATEMENT EXECUTE FUNCTION public.d2_no_mutation();
REVOKE ALL ON FUNCTION public.d2_recon_pending_insert_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_recon_pending_update_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_recon_pending_delete_guard() FROM PUBLIC;
CREATE FUNCTION public.d2_request_reconciliation(
 p_tenant_id TEXT, p_job_id TEXT, p_reason TEXT
) RETURNS public.d2_job_identity_recon_pending
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  gate public.d2_financial_authority_gate%ROWTYPE;
  ident public.d2_job_identity%ROWTYPE;
  result public.d2_job_identity_recon_pending%ROWTYPE;
BEGIN
  -- Lock order: Gate -> pending -> identity -> attempt. Do not reorder.
  IF p_reason IS NULL OR length(btrim(p_reason))=0 THEN
    RAISE EXCEPTION 'reconciliation reason required';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.d2_reinstate_authorized_callers
                 WHERE caller_role=session_user::text) THEN
    RAISE EXCEPTION 'unauthorized reconciliation caller: %', session_user;
  END IF;
  -- Pending blocks new BAR issuance; it does not revoke existing authority.
  SELECT * INTO gate FROM public.d2_financial_authority_gate
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'gate missing'; END IF;
  -- CLOSED gates allow pending creation for late reconciliation; new BARs are still blocked by d2_bar_highwater.
  IF gate.state NOT IN ('OPEN','BLOCKED','CLOSED') THEN
    RAISE EXCEPTION 'reconciliation request requires OPEN, BLOCKED or CLOSED gate';
  END IF;
  PERFORM 1 FROM public.d2_job_identity_recon_pending
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF FOUND THEN RAISE EXCEPTION 'already pending'; END IF;
  SELECT * INTO ident FROM public.d2_job_identity
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'job identity missing'; END IF;
  IF ident.store_kind <> 'PG' OR ident.pg_job_id IS NOT NULL
     OR ident.creation_cancel_intent IS NOT NULL THEN
    RAISE EXCEPTION 'reconciliation request precondition failed';
  END IF;
  INSERT INTO public.d2_recon_pending_insert_permit
    (backend_pid,transaction_id,tenant_id,job_id,requested_by)
    VALUES (pg_backend_pid(),txid_current(),p_tenant_id,p_job_id,session_user);
  INSERT INTO public.d2_job_identity_recon_pending
    (tenant_id,job_id,creation_id,reason,requested_by)
    VALUES (p_tenant_id,p_job_id,ident.creation_id,p_reason,session_user)
    RETURNING * INTO result;
  RETURN result;
END;
$$;
CREATE FUNCTION public.d2_reconcile_creation(
 p_tenant_id TEXT, p_job_id TEXT, p_new_pg_job_id TEXT, p_evidence JSONB
) RETURNS public.d2_job_identity
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_job_identity%ROWTYPE;
  pending public.d2_job_identity_recon_pending%ROWTYPE;
  gate_before public.d2_financial_authority_gate%ROWTYPE;
  gate_after public.d2_financial_authority_gate%ROWTYPE;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.d2_reinstate_authorized_callers
                 WHERE caller_role=session_user::text) THEN
    RAISE EXCEPTION 'unauthorized reconciliation caller: %', session_user;
  END IF;
  -- Lock order: Gate -> pending -> identity -> attempt. Do not reorder.
  IF p_new_pg_job_id IS NULL OR jsonb_typeof(p_evidence) IS DISTINCT FROM 'object' THEN
    RAISE EXCEPTION 'PG job id and object reconciliation evidence required';
  END IF;
  SELECT * INTO gate_before FROM public.d2_financial_authority_gate
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND OR gate_before.state IS DISTINCT FROM 'CLOSED' THEN
    RAISE EXCEPTION 'creation reconciliation requires CLOSED gate';
  END IF;
  SELECT * INTO pending FROM public.d2_job_identity_recon_pending
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'reconciliation pending required'; END IF;
  SELECT * INTO result FROM public.d2_job_identity
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'job identity missing'; END IF;
  IF result.store_kind <> 'PG' OR result.pg_job_id IS NOT NULL
     OR result.creation_cancel_intent IS NOT NULL
     OR pending.creation_id IS DISTINCT FROM result.creation_id THEN
    RAISE EXCEPTION 'creation reconciliation precondition failed';
  END IF;
  UPDATE public.d2_job_identity SET pg_job_id=p_new_pg_job_id
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id
      AND pg_job_id IS NULL AND creation_cancel_intent IS NULL
    RETURNING * INTO result;
  IF NOT FOUND THEN RAISE EXCEPTION 'creation reconciliation CAS lost'; END IF;
  UPDATE public.d2_financial_authority_gate
    SET generation = generation + 1, version = version + 1
    WHERE tenant_id = p_tenant_id AND job_id = p_job_id
      AND state = 'CLOSED';
  IF NOT FOUND THEN RAISE EXCEPTION 'gate generation bump failed'; END IF;
  INSERT INTO public.d2_recon_pending_consume_permit
    (backend_pid,transaction_id,tenant_id,job_id,action)
    VALUES (pg_backend_pid(),txid_current(),p_tenant_id,p_job_id,'RECONCILE');
  DELETE FROM public.d2_job_identity_recon_pending
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'reconciliation pending lost'; END IF;
  SELECT * INTO gate_after FROM public.d2_financial_authority_gate
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id;
  INSERT INTO public.d2_creation_reconciliation_audit
    (tenant_id,job_id,old_pg_job_id,new_pg_job_id,evidence,requested_by,
     generation_before,generation_after,version_before,version_after)
    VALUES (p_tenant_id,p_job_id,NULL,p_new_pg_job_id,p_evidence,session_user,
            gate_before.generation,gate_after.generation,gate_before.version,gate_after.version);
  RETURN result;
END;
$$;
CREATE FUNCTION public.d2_mark_creation_investigation(
 p_tenant_id TEXT, p_job_id TEXT
) RETURNS public.d2_job_identity_recon_pending
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  result public.d2_job_identity_recon_pending%ROWTYPE;
BEGIN
  SELECT * INTO result FROM public.d2_request_reconciliation(p_tenant_id,p_job_id,'LEGACY_COMPAT');
  RETURN result;
END;
$$;
CREATE FUNCTION public.d2_cancel_reconciliation(
 p_tenant_id TEXT, p_job_id TEXT
) RETURNS public.d2_job_identity_recon_pending
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  gate_state TEXT;
  ident public.d2_job_identity%ROWTYPE;
  pending public.d2_job_identity_recon_pending%ROWTYPE;
BEGIN
  -- Lock order: Gate -> pending -> identity -> attempt. Do not reorder.
  IF NOT EXISTS (SELECT 1 FROM public.d2_reinstate_authorized_callers
                 WHERE caller_role=session_user::text) THEN
    RAISE EXCEPTION 'unauthorized reconciliation caller: %', session_user;
  END IF;
  SELECT state INTO gate_state FROM public.d2_financial_authority_gate
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'gate missing'; END IF;
  SELECT * INTO pending FROM public.d2_job_identity_recon_pending
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'reconciliation pending required'; END IF;
  SELECT * INTO ident FROM public.d2_job_identity
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'job identity missing'; END IF;
  IF ident.store_kind='PG' AND ident.pg_job_id IS NULL
     AND (ident.creation_cancel_intent IS NULL OR gate_state <> 'CLOSED') THEN
    RAISE EXCEPTION 'cannot cancel pending without valid creation pair';
  END IF;
  INSERT INTO public.d2_recon_pending_consume_permit
    (backend_pid,transaction_id,tenant_id,job_id,action)
    VALUES (pg_backend_pid(),txid_current(),p_tenant_id,p_job_id,'CANCEL');
  INSERT INTO public.d2_reconciliation_cancel_audit
    (tenant_id,job_id,creation_id,reason,requested_by,requested_at_pg,cancelled_by)
    VALUES (p_tenant_id,p_job_id,pending.creation_id,pending.reason,
            pending.requested_by,pending.requested_at_pg,session_user);
  DELETE FROM public.d2_job_identity_recon_pending
    WHERE tenant_id=p_tenant_id AND job_id=p_job_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'reconciliation pending lost'; END IF;
  RETURN pending;
END;
$$;
REVOKE ALL ON FUNCTION public.d2_cancel_reconciliation(TEXT,TEXT) FROM PUBLIC;

REVOKE ALL ON FUNCTION public.d2_request_reconciliation(TEXT,TEXT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_reconcile_creation(TEXT,TEXT,TEXT,JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.d2_mark_creation_investigation(TEXT,TEXT) FROM PUBLIC;
-- TODO: Substitute actual runtime role name (e.g., roma_app) before deployment
REVOKE ALL ON
 public.d2_reconciliation_cancel_audit,
 public.d2_recon_pending_insert_permit,
 public.d2_recon_pending_consume_permit,
 public.d2_job_identity_recon_pending,
 public.d2_creation_reconciliation_audit,
 public.d2_gate_reinstatement_audit,
 public.d2_reinstate_authorized_callers,
 public.d2_gate_reinstate_permit,
 public.d2_attempt_store,
 public.d2_business_admission_receipt,
 public.d2_local_command_receipt,
 public.d2_job_identity,
 public.d2_financial_authority_gate,
 public.d2_settlement_grant
 FROM "[runtime_role]";
REVOKE ALL ON FUNCTION public.d2_request_reconciliation(TEXT,TEXT,TEXT),
 public.d2_reconcile_creation(TEXT,TEXT,TEXT,JSONB),
 public.d2_mark_creation_investigation(TEXT,TEXT),
 public.d2_cancel_reconciliation(TEXT,TEXT) FROM "[runtime_role]";
COMMIT;
