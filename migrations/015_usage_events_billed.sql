-- 015_usage_events_billed.sql
--
-- `billing/pg_metering.py` и `db_pg_sync.record_usage_event()` пишут
-- `usage_events (tenant_id, event_type, value, cost_usd, job_id, metadata, billed)`
-- начиная с da6fa05, но сама таблица `usage_events` была создана
-- (002_billing_pg.sql) без колонки `billed`. На PG-backed развёртывании
-- каждый metering-insert падал на `UndefinedColumn`, и ни одна usage-строка
-- не записывалась. Это та же природа дефекта, что и в 010_workers_capabilities.
--
-- Семантика колонки — трёхзначная:
--   NULL  — строка до этой миграции, либо событие, биллинг которого ещё не решён;
--   TRUE  — событие списано в ledger (billing/finalize.py);
--   FALSE — событие учтено в метеринге, но не списано (fail-closed путь
--           billing/execution_worker.py: деньги не сохранились → billed не выставляем).
--
-- Backfill отсутствует намеренно: исторические строки остаются NULL,
-- существующие данные не переписываются и не додумываются.
--
-- Идемпотентность: ADD COLUMN IF NOT EXISTS; повторный накат = 0 изменений.
-- Аддитивность: одна новая nullable-колонка; существующие колонки, индексы и
-- данные не трогаются; нет ни UPDATE, ни DELETE, ни DROP, ни TRUNCATE.
-- Секреты: не затрагиваются.

ALTER TABLE usage_events
  ADD COLUMN IF NOT EXISTS billed BOOLEAN DEFAULT NULL;

COMMENT ON COLUMN usage_events.billed IS
  'Billing state of the usage event: NULL = unset/legacy, TRUE = debited in ledger, FALSE = metered but not debited.';
