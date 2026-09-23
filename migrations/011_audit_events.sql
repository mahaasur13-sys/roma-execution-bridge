-- 011_audit_events.sql (reconciling)
-- G-AUDIT-DDL-DRIFT: код пишет/читает `audit_events` (db_adapter.insert_audit_event /
-- audit_event_exists), но DDL таблицы не существовал ни в миграциях, ни в SQLite-
-- бутстрапе db_adapter._ensure_* — на чистой БД запись леджера падала undefined_table.
--
-- RECONCILING (прод-безопасность): на проде таблица могла уже существовать из
-- рантайм-бутстрапа со СВОЕЙ схемой и с историей эпохи double-write. Факт схемы из
-- INSERT db_adapter.py: колонки (id, tenant_id, event_type, entity_type, entity_id,
-- data), data — JSONB, БЕЗ created_at (INSERT её не пишет). Поэтому порядок:
--   (1) create-if-absent; (2) сверка сигнатуры колонок fail-closed (никаких ALTER
--   вслепую); (3) дедуп исторических дублей keep-ранняя (ctid — прод-таблица без
--   временной метки) с логом removed; (4) частичный UNIQUE.

CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    event_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    data        JSONB
);

-- Сверка сигнатуры: каждая колонка, которую пишет/читает код, обязана существовать.
-- Расхождение схемы — STOP с протоколом (fail-closed), никаких ALTER вслепую.
DO $$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(c, ', ' ORDER BY c) INTO missing
    FROM unnest(ARRAY['id','tenant_id','event_type','entity_type','entity_id','data']) AS c
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'audit_events' AND column_name = c
    );
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'audit_events schema mismatch: missing columns [%]', missing;
    END IF;
END $$;

-- Дедуп исторических дублей ДО UNIQUE. keep-ранняя по ctid (прокси порядка вставки:
-- прод-таблица без временной метки). Безключевые (entity_id='unknown', dedupe=False,
-- T2 #93) НЕ трогаются — частичный индекс их не покрывает, они обязаны мирно пережить.
DO $$
DECLARE
    removed int;
BEGIN
    WITH ranked AS (
        SELECT ctid, row_number() OVER (
            PARTITION BY tenant_id, event_type, entity_id ORDER BY ctid
        ) AS rn
        FROM audit_events
        WHERE entity_id IS NOT NULL AND entity_id <> 'unknown'
    )
    DELETE FROM audit_events
    WHERE ctid IN (SELECT ctid FROM ranked WHERE rn > 1);
    GET DIAGNOSTICS removed = ROW_COUNT;
    IF removed > 0 THEN
        RAISE NOTICE 'audit_events dedup: removed % duplicate row(s), keep-earliest by ctid', removed;
    END IF;
END $$;

-- Частичный UNIQUE: идемпотентность ключевых сущностей атомарно (ON CONFLICT DO NOTHING
-- в db_adapter.insert_audit_event). Безключевые события не склеиваются.
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_dedupe_uidx
    ON audit_events (tenant_id, event_type, entity_id)
    WHERE entity_id IS NOT NULL AND entity_id <> 'unknown';
