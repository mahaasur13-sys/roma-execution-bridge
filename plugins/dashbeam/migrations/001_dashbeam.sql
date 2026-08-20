-- DashBeam P2P Transfer Plugin — Initial Schema

CREATE TABLE IF NOT EXISTS dashbeam_tickets (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ticket_token TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    file_hash TEXT NOT NULL,
    mime_type TEXT DEFAULT 'application/octet-stream',
    relay_url TEXT NOT NULL,
    peer_node_id TEXT,
    status TEXT DEFAULT 'pending',
    is_one_time BOOLEAN DEFAULT 1,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX idx_dt_tenant ON dashbeam_tickets(tenant_id);
CREATE INDEX idx_dt_status ON dashbeam_tickets(status);
CREATE INDEX idx_dt_expires ON dashbeam_tickets(expires_at);

CREATE TABLE IF NOT EXISTS dashbeam_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    peer_node_id TEXT NOT NULL,
    device_name TEXT DEFAULT '',
    device_fingerprint TEXT,
    relay_url TEXT NOT NULL,
    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT 1,
    bytes_transferred INTEGER DEFAULT 0,
    transfer_count INTEGER DEFAULT 0
);
CREATE INDEX idx_ds_tenant ON dashbeam_sessions(tenant_id);
CREATE INDEX idx_ds_active ON dashbeam_sessions(is_active);

CREATE TABLE IF NOT EXISTS dashbeam_relay_configs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL UNIQUE,
    relay_url TEXT NOT NULL,
    relay_region TEXT DEFAULT 'auto',
    is_custom BOOLEAN DEFAULT 0,
    max_bandwidth_mbps INTEGER DEFAULT 100,
    discovery_nodes JSON DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_drc_tenant ON dashbeam_relay_configs(tenant_id);

CREATE TABLE IF NOT EXISTS dashbeam_paired_devices (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    device_name TEXT NOT NULL,
    device_fingerprint TEXT NOT NULL UNIQUE,
    peer_node_id TEXT,
    public_key_hash TEXT,
    is_trusted BOOLEAN DEFAULT 0,
    paired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_dpd_tenant ON dashbeam_paired_devices(tenant_id);
