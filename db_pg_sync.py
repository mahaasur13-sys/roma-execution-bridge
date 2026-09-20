#!/usr/bin/env python3
"""Sync PostgreSQL adapter using psycopg2 — for db_adapter.py."""
import logging
import json

logger = logging.getLogger("roma.db_pg_sync")


def init_db(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY, api_key TEXT, name TEXT DEFAULT '',
            plan TEXT DEFAULT 'free', subscription_status TEXT DEFAULT 'inactive',
            stripe_customer_id TEXT DEFAULT '', stripe_subscription_id TEXT DEFAULT '',
            subscription_end_date TEXT, created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tenants_api_key ON tenants(api_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tenants_stripe ON tenants(stripe_customer_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events (
            id SERIAL PRIMARY KEY, stripe_event_id TEXT, event_type TEXT DEFAULT '',
            tenant_id TEXT DEFAULT '', payload TEXT DEFAULT '', created_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_webhook_events_stripe ON webhook_events(stripe_event_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_invoices (
            id SERIAL PRIMARY KEY, invoice_id TEXT UNIQUE, event_type TEXT DEFAULT '',
            tenant_id TEXT DEFAULT '', created_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_processed_invoices_inv ON processed_invoices(invoice_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id SERIAL PRIMARY KEY, email TEXT, company TEXT DEFAULT '',
            role TEXT DEFAULT '', use_case TEXT DEFAULT '', source TEXT DEFAULT '',
            status TEXT DEFAULT 'new', notes TEXT DEFAULT '', created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            id SERIAL PRIMARY KEY, recipient_email TEXT, recipient_name TEXT DEFAULT '',
            tenant_id TEXT DEFAULT '', invitation_link TEXT DEFAULT '',
            status TEXT DEFAULT 'sent', error_message TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_events (
            id SERIAL PRIMARY KEY, tenant_id TEXT, event_type TEXT,
            user_id TEXT DEFAULT '', event_data JSONB DEFAULT '{}',
            ip_address TEXT DEFAULT '', user_agent TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_events_tenant ON user_events(tenant_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY, tenant_id TEXT, user_id TEXT DEFAULT '',
            rating INTEGER, liked TEXT DEFAULT '', improvement TEXT DEFAULT '',
            bug TEXT DEFAULT '', user_agent TEXT DEFAULT '', created_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback(tenant_id)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT, name TEXT DEFAULT '',
            provider TEXT DEFAULT '', tenant_id TEXT DEFAULT '', api_key TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY, tenant_id TEXT DEFAULT '', status TEXT DEFAULT 'idle',
            drained BOOLEAN DEFAULT false,
            capabilities JSONB DEFAULT '{}'::jsonb,
            last_heartbeat TIMESTAMPTZ,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        )
    """)
    # Legacy clusters have `workers` without these columns (register_worker writes
    # both) — keep the bootstrap idempotent instead of failing on an old schema.
    cur.execute("ALTER TABLE workers ADD COLUMN IF NOT EXISTS capabilities JSONB DEFAULT '{}'::jsonb")
    cur.execute("ALTER TABLE workers ADD COLUMN IF NOT EXISTS last_heartbeat TIMESTAMPTZ")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS submit_idempotency_keys (
            tenant_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            job_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY (tenant_id, idempotency_key)
        )
    """)
    conn.commit()


def seed_tenants(conn, api_keys: dict) -> None:
    import hashlib
    cur = conn.cursor()
    for key, info in api_keys.items():
        digest = hashlib.sha256((key or "").encode("utf-8")).hexdigest()
        cur.execute(
            "INSERT INTO tenants (id, api_key, api_key_hash, name, plan, subscription_status) "
            "VALUES (%s,%s,%s,%s,'free','inactive') ON CONFLICT (id) DO NOTHING",
            (info.get("tenant_id", ""), key, digest, info.get("name", info.get("tenant_id", "")))
        )
    conn.commit()


def get_tenant(conn, tenant_id: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None


def update_tenant_subscription(conn, tenant_id: str, stripe_customer_id: str = "",
                               stripe_subscription_id: str = "", subscription_status: str = "",
                               plan: str = "", subscription_end_date=None) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE tenants SET stripe_customer_id=%s, stripe_subscription_id=%s, "
        "subscription_status=%s, plan=%s, subscription_end_date=%s, updated_at=now() "
        "WHERE id=%s",
        (stripe_customer_id, stripe_subscription_id, subscription_status, plan,
         subscription_end_date, tenant_id)
    )
    conn.commit()


def set_tenant_inactive(conn, tenant_id: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE tenants SET subscription_status='inactive', updated_at=now() WHERE id=%s", (tenant_id,))
    conn.commit()


def record_webhook_event(conn, stripe_event_id: str, event_type: str, tenant_id: str, payload: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO webhook_events (stripe_event_id, event_type, tenant_id, payload) VALUES (%s,%s,%s,%s)",
        (stripe_event_id, event_type, tenant_id, payload)
    )
    conn.commit()


def list_webhook_events(conn, limit: int = 20) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM webhook_events ORDER BY created_at DESC LIMIT %s", (limit,))
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def is_invoice_processed(conn, invoice_id: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed_invoices WHERE invoice_id = %s", (invoice_id,))
    return cur.fetchone() is not None


def mark_invoice_processed(conn, invoice_id: str, event_type: str = "", tenant_id: str = "") -> None:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO processed_invoices (invoice_id, event_type, tenant_id) VALUES (%s,%s,%s) ON CONFLICT (invoice_id) DO NOTHING",
        (invoice_id, event_type, tenant_id)
    )
    conn.commit()


def add_lead(conn, email: str, company: str = "", role: str = "", use_case: str = "", source: str = "") -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO leads (email, company, role, use_case, source) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (email, company, role, use_case, source)
    )
    lid = cur.fetchone()[0]
    conn.commit()
    return lid


def list_leads(conn, status: str = "") -> list[dict]:
    cur = conn.cursor()
    if status:
        cur.execute("SELECT * FROM leads WHERE status = %s ORDER BY created_at DESC", (status,))
    else:
        cur.execute("SELECT * FROM leads ORDER BY created_at DESC")
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def update_lead_status(conn, lead_id: int, status: str, notes: str = "") -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE leads SET status=%s, notes=%s, updated_at=now() WHERE id=%s",
        (status, notes, lead_id)
    )
    conn.commit()


def upsert_oauth_user(conn, user_id: str, email: str, name: str, provider: str, tenant_id: str, api_key: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (id, email, name, provider, tenant_id, api_key) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET email=EXCLUDED.email, name=EXCLUDED.name, "
        "updated_at=now() RETURNING *",
        (user_id, email, name, provider, tenant_id, api_key)
    )
    cols = [desc[0] for desc in cur.description]
    return dict(zip(cols, cur.fetchone()))


def get_user_by_id(conn, user_id: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None


def get_user_by_email(conn, email: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None


def get_user_by_api_key(conn, api_key: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE api_key = %s", (api_key,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None


def log_email_sent(conn, recipient_email: str, recipient_name: str, tenant_id: str, invitation_link: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO email_logs (recipient_email, recipient_name, tenant_id, invitation_link, status) "
        "VALUES (%s,%s,%s,%s,'sent') RETURNING id",
        (recipient_email, recipient_name, tenant_id, invitation_link)
    )
    eid = cur.fetchone()[0]
    conn.commit()
    return eid


def log_email_failed(conn, recipient_email: str, error_message: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO email_logs (recipient_email, status, error_message) "
        "VALUES (%s,'failed',%s) RETURNING id",
        (recipient_email, error_message)
    )
    eid = cur.fetchone()[0]
    conn.commit()
    return eid


def update_email_event(conn, recipient_email: str, event_type: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE email_logs SET status=%s WHERE recipient_email=%s AND status='sent'",
        (event_type, recipient_email)
    )
    conn.commit()


def get_email_stats(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM email_logs GROUP BY status")
    stats = {"total": 0, "sent": 0, "failed": 0}
    for status, cnt in cur.fetchall():
        stats[status] = cnt
        stats["total"] += cnt
    return stats


def log_user_event(conn, tenant_id: str, event_type: str, user_id: str = "",
                   event_data: dict = None, ip_address: str = "", user_agent: str = "") -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO user_events (tenant_id, event_type, user_id, event_data, ip_address, user_agent) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (tenant_id, event_type, user_id, json.dumps(event_data or {}), ip_address, user_agent)
    )
    eid = cur.fetchone()[0]
    conn.commit()
    return eid


def get_analytics_overview(conn, days: int = 30) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM user_events WHERE created_at >= now() - interval '%s days'", (days,))
    total = cur.fetchone()[0]
    cur.execute("SELECT count(DISTINCT tenant_id) FROM user_events WHERE created_at >= now() - interval '%s days'", (days,))
    unique = cur.fetchone()[0]
    return {"total_events": total, "unique_tenants": unique, "days": days}


def get_analytics_events(conn, limit: int = 100, offset: int = 0, event_type: str = "",
                          tenant_id: str = "", from_date: str = "", to_date: str = "") -> tuple[list[dict], int]:
    cur = conn.cursor()
    conditions = []
    params = []
    if event_type:
        conditions.append("event_type = %s")
        params.append(event_type)
    if tenant_id:
        conditions.append("tenant_id = %s")
        params.append(tenant_id)
    if from_date:
        conditions.append("created_at >= %s")
        params.append(from_date)
    if to_date:
        conditions.append("created_at <= %s")
        params.append(to_date)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    cur.execute(f"SELECT count(*) FROM user_events {where}", params)
    total = cur.fetchone()[0]

    cur.execute(f"SELECT * FROM user_events {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [limit, offset])
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows, total


def save_feedback(conn, tenant_id: str, user_id: str, rating: int, liked: str = "",
                  improvement: str = "", bug: str = "", user_agent: str = "") -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO feedback (tenant_id, user_id, rating, liked, improvement, bug, user_agent) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (tenant_id, user_id, rating, liked, improvement, bug, user_agent)
    )
    fid = cur.fetchone()[0]
    conn.commit()
    return fid


def get_feedback(conn, limit: int = 50, offset: int = 0, from_date: str = "",
                 to_date: str = "", rating: int = 0) -> tuple[list[dict], int]:
    cur = conn.cursor()
    conditions = []
    params = []
    if from_date:
        conditions.append("created_at >= %s")
        params.append(from_date)
    if to_date:
        conditions.append("created_at <= %s")
        params.append(to_date)
    if rating:
        conditions.append("rating = %s")
        params.append(rating)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    cur.execute(f"SELECT count(*) FROM feedback {where}", params)
    total = cur.fetchone()[0]

    cur.execute(f"SELECT * FROM feedback {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [limit, offset])
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows, total


def get_tenant_workers(conn, tenant_id: str) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM workers WHERE tenant_id = %s ORDER BY created_at DESC", (tenant_id,))
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def drain_worker(conn, worker_id: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE workers SET drained=true, updated_at=now() WHERE id=%s", (worker_id,))
    conn.commit()


def register_worker(conn, worker_id: str, tenant_id: str, capabilities: dict = None) -> None:
    import json
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO workers (id, tenant_id, status, capabilities, last_heartbeat) "
        "VALUES (%s,%s,'idle',%s,now()) "
        "ON CONFLICT (id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id, "
        "status='idle', capabilities=EXCLUDED.capabilities, "
        "last_heartbeat=now(), updated_at=now()",
        (worker_id, tenant_id, json.dumps(capabilities or {}))
    )
    conn.commit()


def update_worker_heartbeat(conn, worker_id: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE workers SET last_heartbeat=now(), updated_at=now() WHERE id=%s", (worker_id,))
    conn.commit()


def release_worker(conn, worker_id: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE workers SET status='idle', updated_at=now() WHERE id=%s", (worker_id,))
    conn.commit()


def get_daily_stats(conn) -> dict:
    """Daily job count + GPU hours over the last 7 days (for /stats/daily)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT to_char(d, 'YYYY-MM-DD') FROM generate_series(now() - interval '7 days', now(), interval '1 day') d"
    )
    dates = [r[0] for r in cur.fetchall()]

    cur.execute(
        "SELECT to_char(created_at, 'YYYY-MM-DD') d, count(*) FROM execution_jobs "
        "WHERE created_at >= now() - interval '7 days' GROUP BY d"
    )
    jobs_by_day = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute(
        "SELECT to_char(created_at, 'YYYY-MM-DD') d, sum(value) FROM usage_events "
        "WHERE created_at >= now() - interval '7 days' AND event_type='gpu_execution' GROUP BY d"
    )
    gpu_by_day = {r[0]: r[1] for r in cur.fetchall()}

    return {
        "dates": dates,
        "jobs_count": [int(jobs_by_day.get(d, 0)) for d in dates],
        "gpu_hours": [round(float(gpu_by_day.get(d, 0)) / 3600.0, 4) for d in dates],
    }


def find_job_by_idempotency(conn, tenant_id: str, idempotency_key: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT job_id FROM submit_idempotency_keys WHERE tenant_id=%s AND idempotency_key=%s",
        (tenant_id, idempotency_key)
    )
    row = cur.fetchone()
    return row[0] if row else None


def create_job_idempotency(conn, tenant_id: str, idempotency_key: str, job_id: str) -> bool:
    """Atomically reserve (tenant_id, idempotency_key) -> job_id.

    Returns True if this call created the mapping, False if the key already
    existed (another identical submit won the race).
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO submit_idempotency_keys (tenant_id, idempotency_key, job_id) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING",
        (tenant_id, idempotency_key, job_id)
    )
    conn.commit()
    return cur.rowcount > 0


def list_tenants(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM tenants ORDER BY created_at DESC")
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── Email Verification ──────────────────────────────────────

def create_user_with_password(conn, user_id: str, email: str, name: str, tenant_id: str, api_key: str, password_hash: str, verification_token: str, token_expires: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (id, email, name, provider, tenant_id, api_key, password_hash, verification_token, verification_token_expires_at, email_verified) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,false) RETURNING *",
        (user_id, email, name, 'email', tenant_id, api_key, password_hash, verification_token, token_expires)
    )
    cols = [desc[0] for desc in cur.description]
    return dict(zip(cols, cur.fetchone()))


def get_user_by_verification_token(conn, token: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE verification_token = %s", (token,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None


def verify_user_email(conn, user_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET email_verified=true, verification_token=NULL, verification_token_expires_at=NULL, updated_at=now() WHERE id=%s",
        (user_id,)
    )
    conn.commit()


def set_verification_token(conn, user_id: str, token: str, expires_at) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET verification_token=%s, verification_token_expires_at=%s, updated_at=now() WHERE id=%s",
        (token, expires_at, user_id)
    )
    conn.commit()


# ── Email Verification ──────────────────────────────────────

def create_user_with_password(conn, user_id: str, email: str, name: str, tenant_id: str, api_key: str, password_hash: str, verification_token: str, token_expires: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO users (id, email, name, provider, tenant_id, api_key, password_hash,
            email_verified, verification_token, verification_token_expires_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
            email=EXCLUDED.email, name=EXCLUDED.name, password_hash=EXCLUDED.password_hash,
            verification_token=EXCLUDED.verification_token,
            verification_token_expires_at=EXCLUDED.verification_token_expires_at,
            updated_at=now()
        RETURNING *""",
        (user_id, email, name, "email", tenant_id, api_key, password_hash, False, verification_token, token_expires)
    )
    cols = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    # Hide password_hash from returned dict
    result = dict(zip(cols, row))
    result.pop("password_hash", None)
    return result


def get_user_by_verification_token(conn, token: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE verification_token = %s", (token,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None


def verify_user_email(conn, user_id: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET email_verified=true, verification_token=NULL, "
        "verification_token_expires_at=NULL, updated_at=now() WHERE id=%s",
        (user_id,)
    )
    conn.commit()


def set_verification_token(conn, user_id: str, token: str, expires_at: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET verification_token=%s, verification_token_expires_at=%s, "
        "updated_at=now() WHERE id=%s",
        (token, expires_at, user_id)
    )
    conn.commit()

def create_email_user(conn, email: str, password_hash: str, name: str, tenant_id: str, api_key: str) -> dict:
    user_id = f"user-{email}"
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (id, email, name, provider, tenant_id, api_key, password_hash, email_verified) "
        "VALUES (%s,%s,%s,'email',%s,%s,%s,false) "
        "ON CONFLICT (id) DO NOTHING RETURNING *",
        (user_id, email, name, tenant_id, api_key, password_hash)
    )
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return get_user_by_email(conn, email)

def mark_email_verified_pg(conn, email: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET email_verified = true, verification_token = NULL, "
        "verification_token_expires_at = NULL, updated_at = now() WHERE email = %s",
        (email,),
    )
    conn.commit()

def get_user_by_verification_token(conn, token_hash: str) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE verification_token = %s "
        "AND verification_token_expires_at > now()",
        (token_hash,),
    )
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None

def store_verification_token(conn, email: str, token_hash: str, expires_at: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET verification_token = %s, verification_token_expires_at = %s, "
        "updated_at = now() WHERE email = %s",
        (token_hash, expires_at, email),
    )
    conn.commit()


# ── Email Verification ────────────────────────────────────────

def create_email_user(conn, user_id: str, email: str, name: str, tenant_id: str, api_key: str, password_hash: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (id, email, name, provider, tenant_id, api_key, password_hash, email_verified) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,false) "
        "ON CONFLICT (id) DO UPDATE SET email=EXCLUDED.email, name=EXCLUDED.name "
        "RETURNING *",
        (user_id, email, name, "email", tenant_id, api_key, password_hash)
    )
    cols = [desc[0] for desc in cur.description]
    return dict(zip(cols, cur.fetchone()))


def set_verification_token(conn, email: str, token: str, expires_at):
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET verification_token=%s, verification_token_expires_at=%s, updated_at=now() "
        "WHERE email=%s",
        (token, expires_at, email)
    )


def find_user_by_verification_token(conn, token: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE verification_token=%s", (token,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None


def mark_email_verified(conn, email: str):
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET email_verified=true, verification_token=NULL, "
        "verification_token_expires_at=NULL, updated_at=now() WHERE email=%s",
        (email,)
    )


def find_user_by_api_key(conn, api_key: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE api_key=%s", (api_key,))
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return None

def update_verification_token(conn, user_id: str, token: str, expires_at: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET verification_token=%s, verification_token_expires_at=%s, updated_at=now() WHERE id=%s",
        (token, expires_at, user_id)
    )
    conn.commit()

# ── Invite Codes ─────────────────────────────────────────────────

def create_invite_code(conn, code: str, created_by: str, max_uses: int, note: str = "", expires_at: str = None) -> dict:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO invite_codes (code, created_by, max_uses, note, expires_at) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (code, created_by, max_uses, note, expires_at)
    )
    cols = [desc[0] for desc in cur.description]
    row = cur.fetchone()
    conn.commit()
    return dict(zip(cols, row))

def validate_invite_code(conn, code: str) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM invite_codes WHERE code=%s AND is_active=true AND "
        "used_count < max_uses AND (expires_at IS NULL OR expires_at > now())",
        (code,)
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [desc[0] for desc in cur.description]
    return dict(zip(cols, row))

def use_invite_code(conn, invite_code_id: int, user_id: str) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE invite_codes SET used_count = used_count + 1 WHERE id=%s", (invite_code_id,))
    cur.execute("INSERT INTO invite_usage (invite_code_id, user_id) VALUES (%s,%s)", (invite_code_id, user_id))
    conn.commit()

def list_invite_codes(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM invite_codes ORDER BY created_at DESC")
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

def deactivate_invite_code(conn, code: str) -> bool:
    cur = conn.cursor()
    cur.execute("UPDATE invite_codes SET is_active=false WHERE code=%s", (code,))
    conn.commit()
    return cur.rowcount > 0

# ── Beta Config ──────────────────────────────────────────────────

def get_beta_config(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM beta_config WHERE id=1")
    row = cur.fetchone()
    if row:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    return {"max_users": 100, "default_spend_cap_usd": 5.00, "is_active": True}

def count_verified_users(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM users WHERE email_verified=true")
    return cur.fetchone()[0]


# ── Usage Events ─────────────────────────────────────────────────

def record_usage_event(conn, tenant_id: str, event_type: str, value: float, cost_usd: float, job_id: str = "", metadata: dict = None, billed: bool = False) -> int:
    import json
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO usage_events (tenant_id, event_type, value, cost_usd, job_id, metadata, billed) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (tenant_id, event_type, value, cost_usd, job_id, json.dumps(metadata or {}), billed)
    )
    eid = cur.fetchone()[0]
    conn.commit()
    return eid
