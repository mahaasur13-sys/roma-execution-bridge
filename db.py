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
    c.execute("PRAGMA mmap_size=33554432")
    c.execute("PRAGMA cache_size=-8000")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA temp_store=MEMORY")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tenants_api_key ON tenants(api_key)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_logs_recipient ON email_logs(recipient_email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_events_tenant ON user_events(tenant_id, event_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id)")
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

        CREATE TABLE IF NOT EXISTS processed_invoices (
            invoice_id   TEXT PRIMARY KEY,
            event_type   TEXT,
            tenant_id    TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_processed_invoices_tenant
            ON processed_invoices(tenant_id);

        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            company TEXT DEFAULT '',
            role TEXT DEFAULT '',
            use_case TEXT DEFAULT '',
            source TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_email TEXT NOT NULL,
            recipient_name TEXT DEFAULT '',
            tenant_id TEXT,
            invitation_link TEXT,
            sent_at TEXT NOT NULL DEFAULT (datetime('now')),
            opened_at TEXT,
            clicked_at TEXT,
            delivered_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_email_logs_status ON email_logs(status);
        CREATE INDEX IF NOT EXISTS idx_email_logs_email ON email_logs(recipient_email);

        CREATE TABLE IF NOT EXISTS user_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            user_id TEXT,
            event_type TEXT NOT NULL,
            event_data TEXT DEFAULT '{}',
            ip_address TEXT,
            user_agent TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_user_events_tenant ON user_events(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_user_events_type ON user_events(event_type);
        CREATE INDEX IF NOT EXISTS idx_user_events_created ON user_events(created_at);

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            user_id TEXT,
            rating INTEGER CHECK(rating >= 1 AND rating <= 5),
            liked TEXT DEFAULT '',
            improvement TEXT DEFAULT '',
            bug TEXT DEFAULT '',
            user_agent TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at);
        CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(rating);

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            name TEXT DEFAULT '',
            provider TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            api_key TEXT NOT NULL,
            avatar_url TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );
        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
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

def add_lead(email: str, company: str = "", role: str = "", use_case: str = "", source: str = "") -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO leads (email, company, role, use_case, source) VALUES (?, ?, ?, ?, ?)",
        (email, company, role, use_case, source),
    )
    lead_id = cur.lastrowid
    c.commit()
    c.close()
    return lead_id


def list_leads(status: str = "") -> list[dict]:
    c = _conn()
    if status:
        rows = c.execute("SELECT * FROM leads WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]


def update_lead_status(lead_id: int, status: str, notes: str = "") -> None:
    c = _conn()
    c.execute(
        "UPDATE leads SET status = ?, notes = ?, updated_at = datetime('now') WHERE id = ?",
        (status, notes, lead_id),
    )
    c.commit()
    c.close()


def upsert_oauth_user(user_id: str, email: str, name: str, provider: str, tenant_id: str, api_key: str) -> dict:
    c = _conn()
    now = datetime.now(timezone.utc).isoformat()
    existing = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if existing:
        c.execute(
            "UPDATE users SET email=?, name=?, updated_at=? WHERE id=?",
            (email, name, now, user_id),
        )
    else:
        c.execute(
            "INSERT INTO users (id, email, name, provider, tenant_id, api_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, email, name, provider, tenant_id, api_key, now, now),
        )
    c.commit()
    row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    c.close()
    return dict(row) if row else {}


def get_user_by_id(user_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    c.close()
    return dict(row) if row else None

def get_user_by_api_key(api_key: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE api_key = ?", (api_key,)).fetchone()
    c.close()
    return dict(row) if row else None


def log_email_sent(recipient_email: str, recipient_name: str, tenant_id: str, invitation_link: str) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO email_logs (recipient_email, recipient_name, tenant_id, invitation_link, status) VALUES (?, ?, ?, ?, 'sent')",
        (recipient_email, recipient_name, tenant_id, invitation_link),
    )
    log_id = cur.lastrowid
    c.commit()
    c.close()
    return log_id


def log_email_failed(recipient_email: str, error_message: str) -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO email_logs (recipient_email, status, error_message) VALUES (?, 'failed', ?)",
        (recipient_email, error_message),
    )
    log_id = cur.lastrowid
    c.commit()
    c.close()
    return log_id


def update_email_event(recipient_email: str, event_type: str) -> None:
    c = _conn()
    now = datetime.now(timezone.utc).isoformat()
    if event_type == 'open':
        c.execute("UPDATE email_logs SET opened_at = ?, status = 'opened' WHERE recipient_email = ? AND opened_at IS NULL", (now, recipient_email))
    elif event_type == 'click':
        c.execute("UPDATE email_logs SET clicked_at = ?, status = 'clicked' WHERE recipient_email = ? AND clicked_at IS NULL", (now, recipient_email))
    elif event_type == 'delivered':
        c.execute("UPDATE email_logs SET delivered_at = ? WHERE recipient_email = ? AND delivered_at IS NULL", (now, recipient_email))
    elif event_type == 'bounce':
        c.execute("UPDATE email_logs SET status = 'bounced' WHERE recipient_email = ?", (recipient_email,))
    c.commit()
    c.close()


def get_email_stats() -> dict:
    c = _conn()
    total = c.execute("SELECT COUNT(*) FROM email_logs").fetchone()[0]
    sent = c.execute("SELECT COUNT(*) FROM email_logs WHERE status != 'pending'").fetchone()[0]
    opened = c.execute("SELECT COUNT(*) FROM email_logs WHERE opened_at IS NOT NULL").fetchone()[0]
    clicked = c.execute("SELECT COUNT(*) FROM email_logs WHERE clicked_at IS NOT NULL").fetchone()[0]
    bounced = c.execute("SELECT COUNT(*) FROM email_logs WHERE status = 'bounced'").fetchone()[0]
    c.close()
    return {
        "total": total, "sent": sent, "opened": opened, "clicked": clicked,
        "bounced": bounced,
        "open_rate": round(opened / sent * 100, 1) if sent > 0 else 0,
        "click_rate": round(clicked / sent * 100, 1) if sent > 0 else 0,
    }


def log_user_event(tenant_id: str, event_type: str, user_id: str = "", event_data: dict = None, ip_address: str = "", user_agent: str = "") -> int:
    c = _conn()
    data_json = json.dumps(event_data or {})
    cur = c.execute(
        "INSERT INTO user_events (tenant_id, user_id, event_type, event_data, ip_address, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, user_id, event_type, data_json, ip_address, user_agent),
    )
    eid = cur.lastrowid
    c.commit()
    c.close()
    return eid


def get_analytics_overview(days: int = 30) -> dict:
    c = _conn()
    cutoff = f"datetime('now', '-{days} days')"
    
    total_users = c.execute("SELECT COUNT(DISTINCT tenant_id) FROM user_events").fetchone()[0]
    active_today = c.execute("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE created_at >= datetime('now', '-1 day')").fetchone()[0]
    active_week = c.execute("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE created_at >= datetime('now', '-7 days')").fetchone()[0]
    active_month = c.execute("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE created_at >= datetime('now', '-30 days')").fetchone()[0]

    login_users = c.execute("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE event_type='login'").fetchone()[0]
    submit_users = c.execute("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE event_type='job_submit'").fetchone()[0]
    login_to_submit = round(submit_users / login_users, 2) if login_users > 0 else 0
    total_submits = c.execute("SELECT COUNT(*) FROM user_events WHERE event_type='job_submit'").fetchone()[0]
    total_completes = c.execute("SELECT COUNT(*) FROM user_events WHERE event_type='job_complete'").fetchone()[0]
    submit_to_complete = round(total_completes / total_submits, 2) if total_submits > 0 else 0

    total_jobs = c.execute("SELECT COUNT(*) FROM user_events WHERE event_type IN ('job_submit', 'job_complete', 'job_failed')").fetchone()[0]
    by_backend = {}
    for row in c.execute("SELECT event_data FROM user_events WHERE event_type='job_submit'").fetchall():
        try:
            d = json.loads(row[0])
            be = d.get("backend", "local")
            by_backend[be] = by_backend.get(be, 0) + 1
        except: pass

    dates = []
    logins = []
    submits = []
    completes = []
    for row in c.execute(f"SELECT date(created_at) as d, event_type, COUNT(*) as cnt FROM user_events WHERE created_at >= {cutoff} GROUP BY 1, 2 ORDER BY 1").fetchall():
        d, et, cnt = row[0], row[1], row[2]
        if d not in dates:
            dates.append(d)
            logins.append(0)
            submits.append(0)
            completes.append(0)
        idx = dates.index(d)
        if et == 'login': logins[idx] = cnt
        elif et == 'job_submit': submits[idx] = cnt
        elif et == 'job_complete': completes[idx] = cnt

    c.close()
    return {
        "total_users": total_users, "active_users_today": active_today,
        "active_users_week": active_week, "active_users_month": active_month,
        "conversion": {"login_to_submit": login_to_submit, "submit_to_complete": submit_to_complete},
        "jobs": {"total": total_jobs, "by_backend": by_backend},
        "events_timeline": {"dates": dates, "logins": logins, "submits": submits, "completes": completes},
    }


def get_analytics_users(start_date: str = "", end_date: str = "", sort_by: str = "last_seen") -> list[dict]:
    c = _conn()
    query = """SELECT tenant_id, MAX(created_at) as last_seen, MIN(created_at) as first_seen,
               COUNT(*) as total_events,
               SUM(CASE WHEN event_type='job_submit' THEN 1 ELSE 0 END) as jobs_submitted,
               SUM(CASE WHEN event_type='job_complete' THEN 1 ELSE 0 END) as jobs_completed
               FROM user_events WHERE 1=1"""
    params = []
    if start_date:
        query += " AND created_at >= ?"
        params.append(start_date)
    if end_date:
        query += " AND created_at <= ?"
        params.append(end_date)
    query += " GROUP BY tenant_id ORDER BY " + ("last_seen DESC" if sort_by == "last_seen" else "total_events DESC")
    rows = c.execute(query, params).fetchall()
    c.close()
    return [{"tenant_id": r[0], "last_seen": r[1], "first_seen": r[2], "total_events": r[3], "jobs_submitted": r[4], "jobs_completed": r[5]} for r in rows]


def get_analytics_events(limit: int = 100, offset: int = 0, event_type: str = "", tenant_id: str = "", from_date: str = "", to_date: str = "") -> tuple[list[dict], int]:
    c = _conn()
    where = ["1=1"]
    params = []
    if event_type:
        where.append("event_type = ?")
        params.append(event_type)
    if tenant_id:
        where.append("tenant_id = ?")
        params.append(tenant_id)
    if from_date:
        where.append("created_at >= ?")
        params.append(from_date)
    if to_date:
        where.append("created_at <= ?")
        params.append(to_date)
    wh = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM user_events WHERE {wh}", params).fetchone()[0]
    rows = c.execute(f"SELECT * FROM user_events WHERE {wh} ORDER BY created_at DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    c.close()
    return [dict(r) for r in rows], total


def save_feedback(tenant_id: str, user_id: str, rating: int, liked: str = "", improvement: str = "", bug: str = "", user_agent: str = "") -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO feedback (tenant_id, user_id, rating, liked, improvement, bug, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, user_id, rating, liked, improvement, bug, user_agent),
    )
    fid = cur.lastrowid
    c.commit()
    c.close()
    return fid


def get_feedback(limit: int = 50, offset: int = 0, from_date: str = "", to_date: str = "", rating: int = 0) -> tuple[list[dict], int]:
    c = _conn()
    where = ["1=1"]
    params = []
    if from_date:
        where.append("created_at >= ?"); params.append(from_date)
    if to_date:
        where.append("created_at <= ?"); params.append(to_date)
    if rating > 0:
        where.append("rating = ?"); params.append(rating)
    wh = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM feedback WHERE {wh}", params).fetchone()[0]
    rows = c.execute(f"SELECT * FROM feedback WHERE {wh} ORDER BY created_at DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    c.close()
    return [dict(r) for r in rows], total




def get_tenant_workers(tenant_id: str) -> list[dict]:
    c = _conn()
    try:
        rows = c.execute("SELECT * FROM workers WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()
def drain_worker(worker_id: str) -> None:
    """Mark a worker status as draining."""
    c = _conn()
    try:
        c.execute("UPDATE workers SET status = 'draining', updated_at = datetime('now') WHERE id = ?", (worker_id,))
        c.commit()
    finally:
        c.close()
def list_tenants() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]


def is_invoice_processed(invoice_id: str) -> bool:
    """Проверяет, был ли уже обработан данный InvoiceId."""
    if not invoice_id:
        return False
    c = _conn()
    try:
        c.execute("SELECT 1 FROM processed_invoices WHERE invoice_id = ? LIMIT 1", (invoice_id,))
        return c.fetchone() is not None
    finally:
        c.close()

def mark_invoice_processed(invoice_id: str, event_type: str = "", tenant_id: str = "") -> None:
    """Помечает InvoiceId как обработанный (идемпотентность)."""
    if not invoice_id:
        return
    c = _conn()
    try:
        c.execute(
            "INSERT OR IGNORE INTO processed_invoices (invoice_id, event_type, tenant_id, processed_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (invoice_id, event_type, tenant_id),
        )
        c.commit()
    finally:
        c.close()
