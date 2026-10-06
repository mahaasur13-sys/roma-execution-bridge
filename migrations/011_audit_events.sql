-- 011_audit_events.sql (reconciling, v4)
-- G-AUDIT-DDL-DRIFT + G-AUDIT-WRITE-ATOMICITY (прод-безопасность, P3.10 аудит #95).
-- На проде таблица audit_events могла существовать из рантайм-бутстрапа со СВОЕЙ
-- схемой и историей эпохи double-write (дубли по (tenant_id, event_type, entity_id)).
-- Порядок: (1) create-if-absent; (2) сверка сигнатуры fail-closed В ОБЕ СТОРОНЫ
-- (имена + типы + PK ровно (id) + лишние NOT NULL-без-default), никаких ALTER вслепую;
-- (3) backup-таблица снятых-дублей (вариант A: только данные); (4) LOCK до индекса
-- включительно; (5) дедуп keep-«ранняя» по ctid из ЖИВОЙ таблицы (append-only,
-- ctid монотонен под локом), NULL-ключи исключены — дедуп согласован с предикатом
-- частичного UNIQUE; (6) cross-check + протокол; (7) частичный UNIQUE.

CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    event_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    data        JSONB
);

-- Backup снятых-дублей (только пользовательские колонки данных; без ctid/created_at).
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
    drift text;
    extra_required text;
    extra_optional text;
    backup_before bigint;
    backup_after bigint;
    newly_backed bigint;
    deleted bigint;
    unknown_before bigint;
    unknown_after bigint;
BEGIN
    -- (2) сверка сигнатуры fail-closed: имена + data_type + udt_name.
    SELECT string_agg(e.c || '(ожидал ' || e.t || ', факт ' ||
                      COALESCE(a.data_type, '<нет>') || '/' || COALESCE(a.udt_name, '<нет>') || ')',
                      '; ' ORDER BY e.c) INTO drift
    FROM (VALUES
        ('id', 'text'), ('tenant_id', 'text'), ('event_type', 'text'),
        ('entity_type', 'text'), ('entity_id', 'text'), ('data', 'jsonb')
    ) AS e(c, t)
    LEFT JOIN information_schema.columns a
        ON a.table_schema = 'public' AND a.table_name = 'audit_events'
       AND a.column_name = e.c
    WHERE a.column_name IS NULL
       OR a.data_type <> e.t
       OR a.udt_name <> e.t;
    IF drift IS NOT NULL THEN
        RAISE EXCEPTION 'audit_events schema drift: %', drift;
    END IF;

    -- (2b) сверка в обе стороны: лишняя NOT NULL-колонка БЕЗ default сломает
    -- 6-колоночный INSERT из db_adapter — fail-closed.
    SELECT string_agg(column_name || '(' || data_type || ')', '; ' ORDER BY column_name)
        INTO extra_required
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'audit_events'
      AND column_name NOT IN ('id','tenant_id','event_type','entity_type','entity_id','data')
      AND is_nullable = 'NO'
      AND column_default IS NULL;
    IF extra_required IS NOT NULL THEN
        RAISE EXCEPTION 'audit_events schema drift: extra required columns (NOT NULL без default) [%]', extra_required;
    END IF;

    -- необязательные доп. колонки допустимы, но регистрируются в выводе мигратора.
    SELECT string_agg(column_name || '(' || data_type || ')', '; ' ORDER BY column_name)
        INTO extra_optional
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'audit_events'
      AND column_name NOT IN ('id','tenant_id','event_type','entity_type','entity_id','data')
      AND (is_nullable = 'YES' OR column_default IS NOT NULL);
    IF extra_optional IS NOT NULL THEN
        RAISE NOTICE 'audit_events: необязательные доп. колонки (допустимы): %', extra_optional;
    END IF;

    -- (2c) PK ровно (id) — составной PK (id, tenant_id) не проходит.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'public' AND tc.table_name = 'audit_events'
          AND tc.constraint_type = 'PRIMARY KEY'
        GROUP BY tc.constraint_name
        HAVING count(kcu.column_name) = 1 AND bool_and(kcu.column_name = 'id')
    ) THEN
        RAISE EXCEPTION 'audit_events schema drift: PRIMARY KEY должен быть ровно (id)';
    END IF;

    -- (4) LOCK: блокирует ROW EXCLUSIVE-записи (INSERT/UPDATE/DELETE) до конца
    -- транзакции, т.е. и на время CREATE UNIQUE INDEX ниже (TOCTOU закрыт).
    LOCK TABLE audit_events IN SHARE ROW EXCLUSIVE MODE;

    SELECT count(*) INTO unknown_before FROM audit_events WHERE entity_id = 'unknown';
    SELECT count(*) INTO backup_before FROM audit_events_dedupe_backup;

    -- (3) idempotent залив снятых-дублей (только ещё не попавших в backup).
    -- NULL-ключи исключены: нет ключа — нет ограничения (семантика T2), дедуп и
    -- частичный UNIQUE согласованы по NULL (PG считает NULL-значения различными).
    INSERT INTO audit_events_dedupe_backup (id, tenant_id, event_type, entity_type, entity_id, data)
    SELECT a.id, a.tenant_id, a.event_type, a.entity_type, a.entity_id, a.data
    FROM (
        SELECT id, tenant_id, event_type, entity_type, entity_id, data,
               row_number() OVER (
                   PARTITION BY tenant_id, event_type, entity_id ORDER BY ctid
               ) AS rn
        FROM audit_events
        WHERE tenant_id IS NOT NULL AND event_type IS NOT NULL
          AND entity_id IS NOT NULL AND entity_id <> 'unknown'
    ) a
    WHERE a.rn > 1
      AND NOT EXISTS (SELECT 1 FROM audit_events_dedupe_backup b WHERE b.id = a.id);
    GET DIAGNOSTICS newly_backed = ROW_COUNT;
    SELECT count(*) INTO backup_after FROM audit_events_dedupe_backup;

    -- (5) DELETE keep-«ранняя» по ctid из живой таблицы (под локом); NULL-ключи исключены.
    WITH ranked AS (
        SELECT ctid, row_number() OVER (
            PARTITION BY tenant_id, event_type, entity_id ORDER BY ctid
        ) AS rn
        FROM audit_events
        WHERE tenant_id IS NOT NULL AND event_type IS NOT NULL
          AND entity_id IS NOT NULL AND entity_id <> 'unknown'
    )
    DELETE FROM audit_events a
    USING ranked r
    WHERE a.ctid = r.ctid AND r.rn > 1;
    GET DIAGNOSTICS deleted = ROW_COUNT;

    -- (6) cross-check: каждый удалённый ряд обязан быть в backup; unknown-пары целы.
    IF deleted <> newly_backed THEN
        RAISE EXCEPTION 'audit_events dedup cross-check: deleted % <> newly-backed-up %',
            deleted, newly_backed;
    END IF;
    SELECT count(*) INTO unknown_after FROM audit_events WHERE entity_id = 'unknown';
    IF unknown_after <> unknown_before THEN
        RAISE EXCEPTION 'audit_events unknown-pairs not intact: % -> %', unknown_before, unknown_after;
    END IF;

    RAISE NOTICE 'audit_events dedup: backup % -> % lines (newly-backed-up %), deleted %, unknown-pairs % -> % (intact)',
        backup_before, backup_after, newly_backed, deleted, unknown_before, unknown_after;
END $$;

-- (7) частичный UNIQUE (внутри той же транзакции под локом; без CONCURRENTLY).
-- Предикат НЕ меняется: для NULL-ключей индекс не конфликтует (PG: NULL различны),
-- дедуп выше исключает NULL-ключи — согласовано.
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_dedupe_uidx
    ON audit_events (tenant_id, event_type, entity_id)
    WHERE entity_id IS NOT NULL AND entity_id <> 'unknown';
