-- Plugin System Migration v1.0.0
-- Tables: plugins, plugin_configs, thought_traces, marketplace_listings

CREATE TABLE IF NOT EXISTS plugins (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    version         TEXT NOT NULL DEFAULT '1.0.0',
    display_name    TEXT NOT NULL,
    description     TEXT DEFAULT '',
    author          TEXT DEFAULT 'ROMA Community',
    category        TEXT NOT NULL DEFAULT 'custom',
    entry_point     TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'registered',
    minimum_tier    TEXT NOT NULL DEFAULT 'free',
    sandbox_policy  TEXT NOT NULL DEFAULT 'restricted',

    dependencies_json   TEXT DEFAULT '[]',
    permissions_json    TEXT DEFAULT '[]',
    config_schema_json  TEXT DEFAULT '{}',
    tags_json           TEXT DEFAULT '[]',

    error_message   TEXT,

    loaded_at   TIMESTAMP,
    enabled_at  TIMESTAMP,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_plugins_name ON plugins(name);
CREATE INDEX IF NOT EXISTS idx_plugins_category ON plugins(category);
CREATE INDEX IF NOT EXISTS idx_plugins_state ON plugins(state);

-- ──────────────────────────────────

CREATE TABLE IF NOT EXISTS plugin_configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_id   TEXT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    tenant_id   TEXT NOT NULL,
    config_json TEXT DEFAULT '{}',
    enabled     BOOLEAN DEFAULT TRUE,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(plugin_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_plugin_configs_plugin ON plugin_configs(plugin_id);
CREATE INDEX IF NOT EXISTS idx_plugin_configs_tenant ON plugin_configs(tenant_id);

-- ──────────────────────────────────

CREATE TABLE IF NOT EXISTS thought_traces (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id            TEXT NOT NULL UNIQUE,
    plugin_name         TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    tenant_id           TEXT NOT NULL DEFAULT '',
    steps_json          TEXT DEFAULT '[]',
    final_decision_json TEXT DEFAULT '{}',
    total_duration_ms   REAL DEFAULT 0.0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_traces_plugin_created ON thought_traces(plugin_name, created_at);
CREATE INDEX IF NOT EXISTS idx_traces_session ON thought_traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_tenant ON thought_traces(tenant_id);

-- ──────────────────────────────────

CREATE TABLE IF NOT EXISTS marketplace_listings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    plugin_name     TEXT NOT NULL REFERENCES plugins(name) ON DELETE CASCADE,
    downloads       INTEGER DEFAULT 0,
    rating          REAL DEFAULT 0.0,
    ratings_count   INTEGER DEFAULT 0,
    installed_count INTEGER DEFAULT 0,
    verified        BOOLEAN DEFAULT FALSE,
    featured        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_marketplace_slug ON marketplace_listings(slug);
CREATE INDEX IF NOT EXISTS idx_marketplace_plugin ON marketplace_listings(plugin_name);

-- ──────────────────────────────────
-- Seed builtin plugins
-- ──────────────────────────────────

INSERT OR IGNORE INTO plugins (id, name, version, display_name, description, author, category, entry_point, state, minimum_tier, sandbox_policy)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'policy-engine', '1.0.0',
     'Policy Engine', 'Enterprise policy evaluation engine',
     'ROMA Core', 'builtin', 'plugins.builtin.policy_engine:PolicyEnginePlugin',
     'registered', 'free', 'restricted'),

    ('00000000-0000-0000-0000-000000000002', 'decision-gate', '1.0.0',
     'Decision Gate', 'Cost gate and quota management',
     'ROMA Core', 'builtin', 'plugins.builtin.decision_gate:DecisionGatePlugin',
     'registered', 'free', 'restricted'),

    ('00000000-0000-0000-0000-000000000003', 'crypto-payments', '1.0.0',
     'Crypto Payments', 'NOWPayments + Monero view-only',
     'ROMA Core', 'builtin', 'plugins.builtin.crypto_payments:CryptoPaymentsPlugin',
     'registered', 'pro', 'network'),

    ('00000000-0000-0000-0000-000000000004', 'support-chat', '1.0.0',
     'AI Support Chat', 'Real-time support + ticket system',
     'ROMA Core', 'builtin', 'plugins.builtin.support_chat:SupportChatPlugin',
     'registered', 'free', 'network');

INSERT OR IGNORE INTO marketplace_listings (slug, plugin_name, verified, featured)
VALUES
    ('policy-engine', 'policy-engine', TRUE, TRUE),
    ('decision-gate', 'decision-gate', TRUE, TRUE),
    ('crypto-payments', 'crypto-payments', TRUE, FALSE),
    ('support-chat', 'support-chat', TRUE, FALSE);
