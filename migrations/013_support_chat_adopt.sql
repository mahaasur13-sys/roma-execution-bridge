-- 013_support_chat_adopt.sql (adoption, Q-B4: «схема вне раннера миграций»)
--
-- Что и зачем. Объекты модуля support_chat (5 таблиц, 8 индексов, 4 enum-типа) жили
-- вне раннера: файл support_chat/migrations/004_support_chat.sql существовал, но схему
-- давал рантайм (create_all при инициализации модуля). Прод-факт на 2026-09-25: таблиц
-- support_* в public нет; в schema_migrations нет записи про 004-support (там другой
-- 004 — 004_invite_codes.sql). Класс дефекта: схема, которую не видит книга миграций.
--
-- Решение: файл-источник 004 НЕ удаляется и НЕ редактируется; настоящая миграция —
-- этот adoption-файл. Он только усыновляет уже известные объекты (CREATE ... IF NOT EXISTS)
-- и доносит недостающие. Никаких DELETE/UPDATE/DROP/TRUNCATE и никаких изменений данных.
--
-- Идемпотентность: повторный накат = 0 изменений (rc=0). CREATE TYPE не имеет
-- IF NOT EXISTS в SQL, поэтому типы закрыты DO-блоком с перехватом duplicate_object.
-- ALTER TYPE audit_event_type — только добавление значения, и только если тип есть.

-- ── enum-типы ────────────────────────────────────────────────────────────────
DO $$
BEGIN
    CREATE TYPE ticket_status AS ENUM ('open', 'in_progress', 'waiting_customer', 'resolved', 'closed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE ticket_priority AS ENUM ('low', 'medium', 'high', 'critical');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE participant_role AS ENUM ('tenant_user', 'tenant_admin', 'support_agent', 'super_admin');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE message_type AS ENUM ('text', 'system', 'internal_note', 'file');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── таблицы (тела = байт-в-байт из support_chat/migrations/004_support_chat.sql) ──
CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    status          ticket_status NOT NULL DEFAULT 'open',
    priority        ticket_priority NOT NULL DEFAULT 'medium',
    assigned_agent_id TEXT,
    context_type    TEXT,
    context_id      UUID,
    context_data    JSONB NOT NULL DEFAULT '{}',
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    tags            JSONB NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS support_messages (
    message_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES support_tickets(ticket_id) ON DELETE CASCADE,
    sender_id       TEXT NOT NULL,
    sender_role     participant_role NOT NULL,
    body            TEXT NOT NULL,
    message_type    message_type NOT NULL DEFAULT 'text',
    is_internal     BOOLEAN NOT NULL DEFAULT FALSE,
    attachment_ids  JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS support_participants (
    participant_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES support_tickets(ticket_id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    role            participant_role NOT NULL,
    tenant_id       TEXT,
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_online       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS support_attachments (
    attachment_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES support_tickets(ticket_id) ON DELETE CASCADE,
    message_id      UUID,
    filename        TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    url             TEXT NOT NULL,
    uploaded_by     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS support_csat_ratings (
    rating_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES support_tickets(ticket_id) ON DELETE CASCADE,
    tenant_id       TEXT NOT NULL,
    score           INTEGER NOT NULL CHECK (score >= 1 AND score <= 5),
    comment         TEXT,
    rated_by        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── индексы ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_support_tickets_tenant ON support_tickets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status);
CREATE INDEX IF NOT EXISTS idx_support_tickets_assigned ON support_tickets(assigned_agent_id);
CREATE INDEX IF NOT EXISTS idx_support_messages_ticket ON support_messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_support_messages_created ON support_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_support_participants_ticket ON support_participants(ticket_id);
CREATE INDEX IF NOT EXISTS idx_support_attachments_ticket ON support_attachments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_support_csat_ticket ON support_csat_ratings(ticket_id);

-- ── расширение enum аудита (только добавление значения; тип приходит из 011) ──
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'audit_event_type') THEN
        ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'support_ticket_created';
    END IF;
END $$;

COMMENT ON TABLE support_tickets IS
    'Adopted by migration 013 (source: support_chat/migrations/004_support_chat.sql; Q-B4). '
    'Схема усыновлена раннером: повторный накат = 0 изменений, данные не затрагиваются.';
