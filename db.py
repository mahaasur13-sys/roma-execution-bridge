"""SQLite database for tenant subscriptions, Stripe data, and webhook events."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "roma.db"


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db() -> None:
    c = _conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            plan TEXT NOT NULL DEFAULT 'free',
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            subscription_status TEXT NOT NULL DEFAULT 'inactive',
            subscription_end_date TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stripe_event_id TEXT UNIQUE,
            event_type TEXT NOT NULL,
            tenant_id TEXT,
            payload TEXT,
            received_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(subscription_status);
        CREATE INDEX IF NOT EXISTS idx_webhooks_tenant ON webhook_events(tenant_id);
    """)
    c.commit()
    c.close()


def seed_tenants(api_keys: dict[str, dict]) -> None:
    c = _conn()
    for key, info in api_keys.items():
        tenant_id = info.get("tenant_id", "")
        name = info.get("name", tenant_id)
        c.execute(
            """INSERT OR IGNORE INTO tenants (id, api_key, name, plan, subscription_status)
               VALUES (?, ?, ?, 'free', 'inactive')""",
            (tenant_id, key, name),
        )
    c.commit()
    c.close()


def get_tenant(tenant_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def update_tenant_subscription(
    tenant_id: str,
    stripe_customer_id: str,
    stripe_subscription_id: str,
    subscription_status: str,
    plan: str = "",
    subscription_end_date: str | None = None,
) -> None:
    c = _conn()
    parts = []
    params = []
    if stripe_customer_id:
        parts.append("stripe_customer_id = ?")
        params.append(stripe_customer_id)
    if stripe_subscription_id:
        parts.append("stripe_subscription_id = ?")
        params.append(stripe_subscription_id)
    if subscription_status:
        parts.append("subscription_status = ?")
        params.append(subscription_status)
    if plan:
        parts.append("plan = ?")
        params.append(plan)
    if subscription_end_date:
        parts.append("subscription_end_date = ?")
        params.append(subscription_end_date)
    parts.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(tenant_id)
    c.execute(f"UPDATE tenants SET {', '.join(parts)} WHERE id = ?", params)
    c.commit()
    c.close()


def set_tenant_inactive(tenant_id: str) -> None:
    c = _conn()
    c.execute(
        "UPDATE tenants SET subscription_status='inactive', updated_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), tenant_id),
    )
    c.commit()
    c.close()


def record_webhook_event(
    stripe_event_id: str, event_type: str, tenant_id: str, payload: str
) -> None:
    c = _conn()
    c.execute(
        "INSERT OR IGNORE INTO webhook_events(stripe_event_id, event_type, tenant_id, payload) VALUES (?,?,?,?)",
        (stripe_event_id, event_type, tenant_id, payload),
    )
    c.commit()
    c.close()


def list_webhook_events(limit: int = 20) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM webhook_events ORDER BY received_at DESC LIMIT ?", (limit,)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def list_tenants() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]
