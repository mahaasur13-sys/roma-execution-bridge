BEGIN;

-- G-MIGRATION-BASIS: базис таблиц, которые миграции правят, но не создают.
--
-- Цепочка миграций была несамодостаточной: users, tenants и workers создавал
-- только бутстрап приложения (db_pg_sync._ensure_schema), а миграции их лишь
-- меняли. На ноде дефект был невидим (таблицы уже существовали из бутстрапа),
-- но на пустой БД CI цепочка не собиралась:
--   003_email_verification → relation "users" does not exist   (CI run 35697239211)
--   005_email_verification → relation "tenants" does not exist (изолированная комната)
--   010_workers_capabilities → ALTER TABLE workers
--
-- DDL ниже повторяет бутстрап PG один в один и идемпотентен
-- (CREATE TABLE / INDEX IF NOT EXISTS), поэтому на существующей нодовской БД
-- применение — no-op без двойного создания. Файл сортируется раньше 003:
-- иначе ALTER снова упирался бы в отсутствующую таблицу.

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT,
    name TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    tenant_id TEXT DEFAULT '',
    api_key TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    api_key TEXT,
    name TEXT DEFAULT '',
    plan TEXT DEFAULT 'free',
    subscription_status TEXT DEFAULT 'inactive',
    stripe_customer_id TEXT DEFAULT '',
    stripe_subscription_id TEXT DEFAULT '',
    subscription_end_date TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tenants_api_key ON tenants(api_key);
CREATE INDEX IF NOT EXISTS idx_tenants_stripe ON tenants(stripe_customer_id);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    drained BOOLEAN DEFAULT false,
    capabilities JSONB DEFAULT '{}'::jsonb,
    last_heartbeat TIMESTAMPTZ,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

COMMIT;
