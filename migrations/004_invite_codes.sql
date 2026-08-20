BEGIN;

-- Invite codes for closed beta
CREATE TABLE IF NOT EXISTS invite_codes (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    created_by      TEXT NOT NULL DEFAULT 'admin',
    max_uses        INTEGER NOT NULL DEFAULT 1,
    used_count      INTEGER NOT NULL DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_invite_codes_code ON invite_codes(code);
CREATE INDEX IF NOT EXISTS idx_invite_codes_active ON invite_codes(is_active);

-- Track which users used which invite code
CREATE TABLE IF NOT EXISTS invite_usage (
    id              BIGSERIAL PRIMARY KEY,
    invite_code_id  BIGINT NOT NULL REFERENCES invite_codes(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    used_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_invite_usage_invite ON invite_usage(invite_code_id);
CREATE INDEX IF NOT EXISTS idx_invite_usage_user ON invite_usage(user_id);

-- Beta config
CREATE TABLE IF NOT EXISTS beta_config (
    id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    max_users       INTEGER NOT NULL DEFAULT 100,
    default_spend_cap_usd DOUBLE PRECISION NOT NULL DEFAULT 5.00,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Insert default beta config if empty
INSERT INTO beta_config (id, max_users, default_spend_cap_usd, is_active)
VALUES (1, 100, 5.00, true)
ON CONFLICT (id) DO NOTHING;

COMMIT;
