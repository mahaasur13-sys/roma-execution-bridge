-- DecisionOS v1.0.0 — Crypto Payments schema
-- Migration 002: adds crypto_payments tables + audit_event_types

CREATE TYPE crypto_network AS ENUM ('USDT_TRC20', 'USDT_ERC20', 'USDC', 'BTC', 'TON', 'SOL');
CREATE TYPE crypto_invoice_status AS ENUM ('pending', 'paid', 'expired', 'cancelled');
CREATE TYPE crypto_payment_status AS ENUM ('detected', 'confirming', 'confirmed', 'failed');

CREATE TABLE IF NOT EXISTS crypto_invoices (
    invoice_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        TEXT NOT NULL,
    tier             TEXT NOT NULL CHECK (tier IN ('start', 'pro', 'enterprise')),
    network          crypto_network NOT NULL,
    amount_usd       NUMERIC(12, 2) NOT NULL,
    amount_crypto    NUMERIC(18, 8) NOT NULL DEFAULT 0,
    pay_address      TEXT NOT NULL,
    provider_invoice_id TEXT NOT NULL,
    status           crypto_invoice_status NOT NULL DEFAULT 'pending',
    tx_hash          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL,
    paid_at          TIMESTAMPTZ,
    CONSTRAINT fk_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS crypto_payments (
    payment_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id       UUID NOT NULL REFERENCES crypto_invoices(invoice_id) ON DELETE CASCADE,
    tenant_id        TEXT NOT NULL,
    tier             TEXT NOT NULL,
    amount_usd       NUMERIC(12, 2) NOT NULL,
    amount_crypto    NUMERIC(18, 8) NOT NULL,
    tx_hash          TEXT NOT NULL,
    network          crypto_network NOT NULL,
    status           crypto_payment_status NOT NULL DEFAULT 'detected',
    paid_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crypto_webhook_events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id       UUID REFERENCES crypto_invoices(invoice_id) ON DELETE SET NULL,
    event_type       TEXT NOT NULL,
    raw_payload      JSONB NOT NULL DEFAULT '{}',
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crypto_invoices_tenant ON crypto_invoices(tenant_id);
CREATE INDEX IF NOT EXISTS idx_crypto_invoices_status ON crypto_invoices(status);
CREATE INDEX IF NOT EXISTS idx_crypto_payments_invoice ON crypto_payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_crypto_payments_tx_hash ON crypto_payments(tx_hash);
CREATE INDEX IF NOT EXISTS idx_crypto_webhook_invoice ON crypto_webhook_events(invoice_id);

ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_invoice_created';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_invoice_paid';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_invoice_expired';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_webhook_received';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_webhook_verified';
ALTER TYPE audit_event_type ADD VALUE IF NOT EXISTS 'crypto_tier_activated';
