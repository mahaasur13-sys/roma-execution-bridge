-- 012_audit_events_reconcile_order.sql
-- G-AUDIT-MIGRATION-HARDENING (PR #95 thread :50): ctid — физическая позиция версии
-- строки, а не порядок создания. Повторный согласующий дедуп с достоверным порядком
-- (created_at, id); 011 НЕ редактируется (уже применён). Идемпотентен: повтор = 0.

CREATE TABLE IF NOT EXISTS audit_events_dedupe_backup (
    id          TEXT,
    tenant_id   TEXT,
    event_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    data        JSONB
);

DO $$
DECLARE
    has_created_at boolean;
    order_expr text;
    backup_before bigint;
    backup_after bigint;
    newly_backed bigint;
    deleted bigint;
BEGIN
    -- прод-форма рантайм-бутстрапа имеет created_at; каноническая 6-колоночная — нет.
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'audit_events'
          AND column_name = 'created_at'
    ) INTO has_created_at;
    order_expr := CASE WHEN has_created_at THEN 'created_at, id' ELSE 'id' END;

    -- LOCK до конца транзакции (дисциплина 011) — записи исключены на время дедупа.
    LOCK TABLE audit_events IN SHARE ROW EXCLUSIVE MODE;

    SELECT count(*) INTO backup_before FROM audit_events_dedupe_backup;

    -- (1) backup снятых-дублей (только ещё не попавших); авто-колонки не вставляются.
    EXECUTE format($f$
        INSERT INTO audit_events_dedupe_backup (id, tenant_id, event_type, entity_type, entity_id, data)
        SELECT a.id, a.tenant_id, a.event_type, a.entity_type, a.entity_id, a.data
        FROM (
            SELECT id, tenant_id, event_type, entity_type, entity_id, data,
                   row_number() OVER (
                       PARTITION BY tenant_id, event_type, entity_id ORDER BY %s
                   ) AS rn
            FROM audit_events
            WHERE tenant_id IS NOT NULL AND event_type IS NOT NULL
              AND entity_id IS NOT NULL AND entity_id <> 'unknown'
        ) a
        WHERE a.rn > 1
          AND NOT EXISTS (SELECT 1 FROM audit_events_dedupe_backup b WHERE b.id = a.id)
    $f$, order_expr);
    GET DIAGNOSTICS newly_backed = ROW_COUNT;
    SELECT count(*) INTO backup_after FROM audit_events_dedupe_backup;

    -- (2) DELETE дублей: survivor = самое раннее по (created_at, id) / (id).
    EXECUTE format($f$
        DELETE FROM audit_events a
        USING (
            SELECT id, row_number() OVER (
                PARTITION BY tenant_id, event_type, entity_id ORDER BY %s
            ) AS rn
            FROM audit_events
            WHERE tenant_id IS NOT NULL AND event_type IS NOT NULL
              AND entity_id IS NOT NULL AND entity_id <> 'unknown'
        ) r
        WHERE a.id = r.id AND r.rn > 1
    $f$, order_expr);
    GET DIAGNOSTICS deleted = ROW_COUNT;

    -- (3) cross-check: каждый удалённый ряд обязан быть в backup.
    IF deleted <> newly_backed THEN
        RAISE EXCEPTION 'audit_events dedup 012 cross-check: deleted % <> newly-backed-up %',
            deleted, newly_backed;
    END IF;

    RAISE NOTICE 'audit_events dedup 012: order=%, backup % -> % (newly %), deleted %',
        order_expr, backup_before, backup_after, newly_backed, deleted;
END $$;
