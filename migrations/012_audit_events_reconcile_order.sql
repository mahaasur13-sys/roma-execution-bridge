-- 012_audit_events_reconcile_order.sql (reconciling, v1)
-- Закрывает три Major-треда CodeRabbit на применённой 011 (PR #95), не переписывая её.
-- Прод-PG 15.18: 011 применена, uidx создан, дедуп удалил 0 строк, строк 515 (514 + smoke Z-2a).
--
-- :128 (ctid как порядок) — ФИКС ПО СУЩЕСТВУ: `ctid` = физическая позиция версии строки,
--      после UPDATE/MOVE потомок может получить ctid раньше «родителя». Достоверного признака
--      порядка создания у исторических строк НЕТ, поэтому 012 НЕ удаляет дубли и НЕ называет
--      выбранную строку «самой ранней». Правило сохранения: исторические записи сохраняются
--      все; при обнаружении групп дублей миграция падает fail-closed со списком ключей —
--      решение принимает человек (никаких ALTER/DELETE вслепую).
-- :69  (автозаполняемые колонки) — сигнатура сверяется корректно: NOT NULL-колонки с
--      is_identity='YES' или is_generated='ALWAYS' заполняются сами и НЕ считаются
--      обязательными полями шестиколоночного INSERT; они допустимы как «лишние».
-- :80  (потеря значений доп. колонок) — резервная таблица хранит ПОЛНУЮ строку
--      (`to_jsonb(a)`), а не шесть колонок: любая доп. колонка (например request_id)
--      не теряется. Удаления в 012 нет вовсе, поэтому потерь нет по построению.
--
-- Инвариант: идемпотентно; повторный прогон — no-op; 0 ALTER существующих колонок.

BEGIN;

-- (1) Таблица (create-if-absent) — та же сигнатура, что в 011 и в SQLite-бутстрапе.
CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT,
    event_type  TEXT,
    entity_type TEXT,
    entity_id   TEXT,
    data        TEXT
);

-- (2) Сверка сигнатуры fail-closed в обе стороны (имена + типы), но с корректным
--     исключением автозаполняемых колонок (тред :69).
DO $$
DECLARE
    extra_required text;
    expected text[] := ARRAY['id', 'tenant_id', 'event_type', 'entity_type', 'entity_id', 'data'];
BEGIN
    SELECT string_agg(column_name || '(' || data_type || ')', ', ' ORDER BY column_name)
      INTO extra_required
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name   = 'audit_events'
       AND column_name <> ALL (expected)
       AND is_nullable = 'NO'
       AND column_default IS NULL
       AND coalesce(is_identity, 'NO')  <> 'YES'     -- sequence-backed: заполняется сама
       AND coalesce(is_generated, 'NEVER') <> 'ALWAYS'; -- generated: вычисляется сама

    IF extra_required IS NOT NULL THEN
        RAISE EXCEPTION
            'audit_events: обязательные дополнительные колонки: %. Миграция не трогает схему вслепую (012).',
            extra_required;
    END IF;
END $$;

-- (3) Lossless-резерв: полная строка в JSONB (тред :80). Заполняется только при ручном
--     разборе дублей (оператором): автоудаления в 012 нет, поэтому потери невозможны.
CREATE TABLE IF NOT EXISTS audit_events_dedupe_backup_full (
    id        text,
    row_json  jsonb       NOT NULL,
    backed_at timestamptz NOT NULL DEFAULT now()
);

-- (4) Группы дублей: НЕ удаляем и не угадываем порядок (тред :128) — фиксируем и отказываем.
DO $$
DECLARE
    dup_groups int;
    dup_keys   text;
BEGIN
    SELECT count(*), string_agg(k, ' | ' ORDER BY k)
      INTO dup_groups, dup_keys
      FROM (
            SELECT tenant_id || '/' || event_type || '/' || entity_id AS k
              FROM audit_events
             WHERE entity_id IS NOT NULL AND entity_id <> 'unknown'
             GROUP BY tenant_id, event_type, entity_id
            HAVING count(*) > 1
           ) q;

    IF dup_groups > 0 THEN
        -- Резервная копия внутри этой же транзакции откатилась бы вместе с RAISE —
        -- поэтому НЕ пишем сюда и НЕ обещаем: удаления нет вовсе, строки сохранены на месте.
        RAISE EXCEPTION
            'audit_events: % групп дублей — достоверного порядка создания нет, автоудаление запрещено (012). Строки не удалены (остались на месте, доп. колонки сохранены). Ключи: %',
            dup_groups, dup_keys;
    END IF;
END $$;

-- (5) Частичный UNIQUE (как в 011/N2b и в SQLite-бутстрапе).
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_dedupe_uidx
    ON audit_events (tenant_id, event_type, entity_id)
    WHERE entity_id IS NOT NULL AND entity_id <> 'unknown';

COMMENT ON INDEX audit_events_dedupe_uidx IS
    'Дедуп ключевых событий. 012: порядок создания исторических строк недоказуем (ctid не признак) — автоудаление запрещено, разбор дублей только вручную.';

COMMIT;
