-- DecisionOS v1.0.0 — Support Chat schema
-- Migration 004: adds support_tickets, messages, participants, attachments, CSAT

CREATE TYPE ticket_status AS ENUM ('open', 'in_progress', 'waiting_customer', 'resolved', 'closed');
CREATE TYPE ticket_priority AS ENUM ('low', 'medium', 'high', 'critical');
CREATE TYPE participant_role AS ENUM ('tenant_user', 'tenant_admin', 'support_agent', 'super_admin');
CREATE TYPE message_type AS ENUM ('text', 'system', 'internal_note', 'file');

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

CREATE INDEX IF NOT EXISTS idx_support_tickets_tenant ON support_tickets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status);
CREATE INDEX IF NOT EXISTS idx_support_tickets_assigned ON support_tickets(assigned_agent_id);
CREATE INDEX IF NOT EXISTS idx_support_messages_ticket ON support_messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_support_messages_created ON support_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_support_participants_ticket ON support_participants(ticket_id);
CREATE INDEX IF NOT EXISTS idx_support_attachments_ticket ON support_attachments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_support_csat_ticket ON support_csat_ratings(ticket_id);

ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'support_ticket_created';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'support_message_sent';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'support_agent_assigned';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'support_ticket_resolved';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'support_csat_submitted';
