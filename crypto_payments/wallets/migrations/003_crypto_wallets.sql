-- DecisionOS v1.0.0 — Crypto Wallets schema
-- Migration 003: adds wallet management + audit_event_types

CREATE TYPE crypto_wallet_type AS ENUM ('provider', 'self_hosted', 'hardware', 'monero');
CREATE TYPE crypto_wallet_mode AS ENUM ('hot', 'view_only', 'cold');
CREATE TYPE crypto_wallet_status AS ENUM ('active', 'rotating', 'retired', 'compromised');

CREATE TABLE IF NOT EXISTS crypto_wallets (
    wallet_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL,
    label           TEXT NOT NULL,
    wallet_type     crypto_wallet_type NOT NULL,
    mode            crypto_wallet_mode NOT NULL DEFAULT 'view_only',
    provider        TEXT,
    public_address  TEXT,
    rpc_endpoint    TEXT,
    view_key        TEXT,
    status          crypto_wallet_status NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at      TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS crypto_deposit_addresses (
    address_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id          UUID NOT NULL REFERENCES crypto_wallets(wallet_id) ON DELETE CASCADE,
    invoice_id         UUID REFERENCES crypto_invoices(invoice_id) ON DELETE SET NULL,
    currency           TEXT NOT NULL,
    network            TEXT NOT NULL,
    address            TEXT NOT NULL,
    subaddress_index   INTEGER,
    is_monero_subaddress BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ,
    used               BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS crypto_monero_subaddresses (
    id                SERIAL PRIMARY KEY,
    wallet_id         UUID NOT NULL REFERENCES crypto_wallets(wallet_id) ON DELETE CASCADE,
    account_index     INTEGER NOT NULL DEFAULT 0,
    subaddress_index  INTEGER NOT NULL,
    address           TEXT NOT NULL,
    label             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_wallet_rotations (
    rotation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id     UUID NOT NULL REFERENCES crypto_wallets(wallet_id) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL,
    old_address   TEXT,
    new_address   TEXT,
    reason        TEXT NOT NULL,
    rotated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    performed_by  TEXT NOT NULL DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_crypto_wallets_tenant ON crypto_wallets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_crypto_wallets_type ON crypto_wallets(wallet_type);
CREATE INDEX IF NOT EXISTS idx_crypto_wallets_status ON crypto_wallets(status);
CREATE INDEX IF NOT EXISTS idx_deposit_addresses_wallet ON crypto_deposit_addresses(wallet_id);
CREATE INDEX IF NOT EXISTS idx_deposit_addresses_invoice ON crypto_deposit_addresses(invoice_id);
CREATE INDEX IF NOT EXISTS idx_monero_subaddresses_wallet ON crypto_monero_subaddresses(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wallet_rotations_wallet ON crypto_wallet_rotations(wallet_id);

ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_wallet_created';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_wallet_address_generated';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_monero_subaddress_created';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_wallet_rotated';
