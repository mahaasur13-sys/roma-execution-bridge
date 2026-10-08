-- 014_support_identity_h2a.sql (H2a: trusted support identity, membership, credential, session)
--
-- Что и зачем. H1 закрыл support_chat каноническим API-ключом, но API-ключ
-- идентифицирует тенанта, а не человека: assign/transition остались fail-closed
-- 403, потому что доверенной роли взять негде. H2a добавляет отдельную
-- authority — support-идентичность с явными членствами в тенантах и
-- hash-only credential/session. Ключи тенантов как человеческие identity
-- НЕ переиспользуются.
--
-- Идемпотентность: CREATE TABLE/INDEX ... IF NOT EXISTS; повторный накат = 0
-- изменений. Аддитивность: только новые таблицы, существующие support_* таблицы
-- и данные не трогаются; нет ни DELETE/UPDATE/DROP/TRUNCATE, ни ALTER.
-- Seed-данных нет: ни одной INSERT — ни identity, ни credential, ни membership.
--
-- Секреты: в БД попадают только SHA-256 дайджесты (credential_hash,
-- session_hash, csrf_hash). Сырые credential/session/CSRF не хранятся и не
-- логируются; session_* / credential_* — high-entropy токены ≥256 бит.

-- ── support_identities ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS support_identities (
    identity_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject         TEXT NOT NULL,
    display_name    TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT support_identities_subject_uniq UNIQUE (subject)
);

-- ── support_identity_memberships (явные членства, без wildcard) ─────────────
CREATE TABLE IF NOT EXISTS support_identity_memberships (
    membership_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id   UUID NOT NULL
        REFERENCES support_identities (identity_id) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL,
    role          TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT support_identity_memberships_tenant_not_empty
        CHECK (length(trim(tenant_id)) > 0),
    CONSTRAINT support_identity_memberships_role_h2a
        CHECK (role = 'support_agent'),
    CONSTRAINT support_identity_memberships_uniq
        UNIQUE (identity_id, tenant_id, role)
);

CREATE INDEX IF NOT EXISTS idx_support_identity_memberships_identity
    ON support_identity_memberships (identity_id);

-- ── support_credentials (hash-only, обязательный expiry) ────────────────────
CREATE TABLE IF NOT EXISTS support_credentials (
    credential_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id     UUID NOT NULL
        REFERENCES support_identities (identity_id) ON DELETE CASCADE,
    credential_hash TEXT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    CONSTRAINT support_credentials_hash_uniq UNIQUE (credential_hash)
);

CREATE INDEX IF NOT EXISTS idx_support_credentials_identity
    ON support_credentials (identity_id);

-- ── support_sessions (server-side, hash-only, fixed 1h TTL) ─────────────────
CREATE TABLE IF NOT EXISTS support_sessions (
    session_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id     UUID NOT NULL
        REFERENCES support_identities (identity_id) ON DELETE CASCADE,
    session_hash    TEXT NOT NULL,
    csrf_hash       TEXT NOT NULL,
    session_version INTEGER NOT NULL,
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    CONSTRAINT support_sessions_hash_uniq UNIQUE (session_hash)
);

CREATE INDEX IF NOT EXISTS idx_support_sessions_identity
    ON support_sessions (identity_id);
