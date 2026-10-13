-- 016_usage_events_idempotency.sql  (rev. 3 — sentinel-excluding predicate, no billed-state condition)
--
-- Единая точка биллинга `billing/finalize.py` пишет usage_events ровно тогда,
-- когда условный DEBIT в ledger уже сохранён (строки 92 и 107 — классы ресурсов
-- gpu/токены). До сих пор ничто на уровне схемы не мешало записать вторую такую
-- же (tenant_id, job_id, event_type) строку: повторный проход finalize по тому
-- же job/классу ресурса приводил к двойному списанию, которое видно в
-- usage_events, но не имеет ни одного барьера в БД.
--
-- Задача миграции — идемпотентность на уровне схемы: один факт учёта на
-- (tenant_id, job_id, event_type) для реального job_id.
--
-- Предикат (rev. 3): WHERE job_id NOT IN ('manual', 'auto')
--   * Реальный job_id (UUID) — входит в ключ; повторный insert падает
--     UniqueViolation, и это и есть сигнал о двойном списании.
--   * Sentinel job_id 'manual'/'auto' исключён из ключа. `billing/finalize.py:91,106`
--     пишет job_id = job_id or "auto", когда вызывающий не передал job id;
--     `billing/metering.py:43` и `billing/pg_metering.py:72` имеют default
--     job_id="manual". Оба литерала не идентифицируют job — они повторяются
--     у разных job'ов и тенантов (live на момент ревизии: 30 строк job_id='auto'
--     в 5 тенантах). Включение sentinel'а в ключ запретило бы второй легитимный
--     факт и разрушило бы multi-tenant изоляцию, которая держится на tenant_id.
--   * job_id IS NULL также вне индекса: `NULL NOT IN (...)` не истинно, строка
--     предикатом отсечена. Это согласовано с писателями — billed-путь finalize
--     никогда не пишет NULL job_id (всегда job_id or "auto").
--   * Состояние billed в предикат НЕ входит. Ранние ревизии (rev. 1/2) добавляли
--     `AND billed IS TRUE`; на live-данных это давало пустой индекс (все 39 строк
--     имеют billed IS NULL) и не защищало ни от одного дубля — insert дубля с
--     billed = NULL проходил. Предикат rev. 3 применяет защиту независимо от
--     billed (NULL/FALSE/TRUE), что и соответствует фактическому поведению
--     писателей, которые не выставляют billed перед вставкой.
--
-- Мультитенантность: tenant_id входит в ключ первым, cross-tenant коллизии
-- невозможны.
--
-- Идемпотентность: DROP INDEX IF EXISTS + CREATE UNIQUE INDEX; повторный накат
-- = 0 изменений. Транзакцию открывает раннер (в файле нет BEGIN/COMMIT);
-- DROP и CREATE идут в одной транзакции, поэтому неудачный CREATE откатывает и DROP.
-- Аддитивность: заменяется ровно один индекс; колонки, данные, остальные индексы
-- и гранты не трогаются; нет ни ALTER COLUMN, ни UPDATE, ни DELETE, ни TRUNCATE,
-- ни REVOKE, ни CREATE ROLE. Backfill отсутствует.
-- Секреты: не затрагиваются.
--
-- Совместимость с писателями: `billing/finalize.py` и `db_pg_sync.record_usage_event()`
-- выполняют обычный INSERT без ON CONFLICT. Ограничение не меняет поведение
-- успешного пути; при попытке второго учёта по тому же (tenant_id, job_id,
-- event_type) с реальным job_id insert падает UniqueViolation — это и есть сигнал
-- о двойном списании. Приведение писателей к ON CONFLICT DO NOTHING — отдельное
-- решение, вне этой миграции.
--
-- Блокировки: таблица мала (39 строк), построение индекса занимает ACCESS EXCLUSIVE
-- на миллисекунды; раннер накатывает файл внутри одной транзакции, поэтому
-- SET LOCAL lock_timeout действует на весь файл. Сервис не перезапускается.

SET LOCAL lock_timeout = '5s';

DROP INDEX IF EXISTS ux_usage_events_billed_idempotency;

CREATE UNIQUE INDEX ux_usage_events_billed_idempotency
  ON usage_events (tenant_id, job_id, event_type)
  WHERE job_id NOT IN ('manual', 'auto');

COMMENT ON INDEX ux_usage_events_billed_idempotency IS
  'Idempotency guard: at most one usage event per (tenant_id, job_id, event_type). Sentinel job_ids (manual/auto) and NULL job_id are excluded; no billed-state condition (rev. 3).';
