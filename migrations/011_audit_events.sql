-- 011_audit_events.sql
-- G-AUDIT-DDL-DRIFT: код пишет/читает `audit_events` (db_adapter.insert_audit_event /
-- audit_event_exists), но DDL таблицы не существовал ни в миграциях, ни в SQLite-
-- бутстрапе db_adapter._ensure_* — на чистой БД (CI postgres:16, свежая data/roma.db)
-- запись аудит-леджера падала undefined_table, а «живой» тест гонялся через фикстуру,
-- создававшую таблицу вручную. Миграция даёт таблицу-источник и закрывает дыру.
-- Идемпотентно: CREATE TABLE/INDEX IF NOT EXISTS (стиль 000_basis_schema.sql).

CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- G-AUDIT-WRITE-ATOMICITY: идемпотентность на уровне БД по ключу
-- (tenant_id, event_type, entity_id) — как в audit.event_store.write_event_once.
-- Частичный: безключевые события (entity_id='unknown', dedupe=False, T2 #93)
-- не склеиваются — иначе два подтверждения задачи без job_id слились бы в один
-- под общим 'unknown'. Ключевые сущности дедуплицируются атомарно (ON CONFLICT).
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_dedupe_uidx
    ON audit_events (tenant_id, event_type, entity_id)
    WHERE entity_id IS NOT NULL AND entity_id <> 'unknown';

CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_ts
    ON audit_events (tenant_id, created_at DESC);
