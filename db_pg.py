"""PostgreSQL database adapter for ROMA Execution Bridge.

Replaces SQLite-based db.py when PG_DSN environment variable is set.
Falls back to SQLite db.py for zero-downtime migration.

Usage:
    from db_pg import get_pool, init_db

    await init_db()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE id=$1", tenant_id)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg

logger = logging.getLogger("roma.db_pg")

POOL: Optional[asyncpg.Pool] = None

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/roma"


def _dsn() -> str:
    return os.environ.get("PG_DSN", DEFAULT_DSN)


async def get_pool() -> asyncpg.Pool:
    global POOL
    if POOL is None:
        POOL = await asyncpg.create_pool(
            dsn=_dsn(),
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("PostgreSQL pool created: %s", _dsn())
    return POOL


async def close_pool() -> None:
    global POOL
    if POOL:
        await POOL.close()
        POOL = None
        logger.info("PostgreSQL pool closed")


async def init_db() -> None:
    """Apply schema from migrations/001_initial_schema.sql."""
    pool = await get_pool()
    schema_path = Path(__file__).parent / "migrations" / "001_initial_schema.sql"
    sql = schema_path.read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    logger.info("PostgreSQL schema applied: %s", schema_path)


# ── Tenant CRUD ─────────────────────────────────────────────

async def get_tenant(tenant_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant_id)
    return dict(row) if row else None


async def update_tenant_subscription(
    tenant_id: str,
    stripe_customer_id: str = "",
    stripe_subscription_id: str = "",
    subscription_status: str = "",
    plan: str = "",
    subscription_end_date: str | None = None,
) -> None:
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        parts = ["updated_at = $1"]
        params = [now]
        idx = 2
        if stripe_customer_id:
            parts.append(f"stripe_customer_id = ${idx}"); params.append(stripe_customer_id); idx += 1
        if stripe_subscription_id:
            parts.append(f"stripe_subscription_id = ${idx}"); params.append(stripe_subscription_id); idx += 1
        if subscription_status:
            parts.append(f"subscription_status = ${idx}"); params.append(subscription_status); idx += 1
        if plan:
            parts.append(f"plan = ${idx}"); params.append(plan); idx += 1
        if subscription_end_date:
            parts.append(f"subscription_end_date = ${idx}"); params.append(subscription_end_date); idx += 1
        params.append(tenant_id)
        await conn.execute(
            f"UPDATE tenants SET {', '.join(parts)} WHERE id = ${idx}", params
        )


async def set_tenant_inactive(tenant_id: str) -> None:
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET subscription_status='inactive', updated_at=$1 WHERE id=$2",
            now, tenant_id,
        )


async def list_tenants() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM tenants ORDER BY created_at DESC")
    return [dict(r) for r in rows]


# ── Webhook Events ──────────────────────────────────────────

async def record_webhook_event(stripe_event_id: str, event_type: str, tenant_id: str, payload: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO webhook_events(stripe_event_id, event_type, tenant_id, payload) VALUES ($1,$2,$3,$4) ON CONFLICT (stripe_event_id) DO NOTHING",
            stripe_event_id, event_type, tenant_id, payload,
        )


async def list_webhook_events(limit: int = 20) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM webhook_events ORDER BY received_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]


# ── Processed Invoices ──────────────────────────────────────

async def is_invoice_processed(invoice_id: str) -> bool:
    if not invoice_id:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM processed_invoices WHERE invoice_id = $1 LIMIT 1", invoice_id)
    return row is not None


async def mark_invoice_processed(invoice_id: str, event_type: str = "", tenant_id: str = "") -> None:
    if not invoice_id:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO processed_invoices (invoice_id, event_type, tenant_id, processed_at) VALUES ($1,$2,$3,now()) ON CONFLICT (invoice_id) DO NOTHING",
            invoice_id, event_type, tenant_id,
        )


# ── Leads ───────────────────────────────────────────────────

async def add_lead(email: str, company: str = "", role: str = "", use_case: str = "", source: str = "") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO leads (email, company, role, use_case, source) VALUES ($1,$2,$3,$4,$5) RETURNING id",
            email, company, role, use_case, source,
        )
    return row["id"]


async def list_leads(status: str = "") -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch("SELECT * FROM leads WHERE status = $1 ORDER BY created_at DESC", status)
        else:
            rows = await conn.fetch("SELECT * FROM leads ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def update_lead_status(lead_id: int, status: str, notes: str = "") -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE leads SET status=$1, notes=$2, updated_at=now() WHERE id=$3",
            status, notes, lead_id,
        )


# ── Users ───────────────────────────────────────────────────

async def upsert_oauth_user(user_id: str, email: str, name: str, provider: str, tenant_id: str, api_key: str) -> dict:
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if existing:
            await conn.execute(
                "UPDATE users SET email=$1, name=$2, updated_at=$3 WHERE id=$4",
                email, name, now, user_id,
            )
        else:
            await conn.execute(
                "INSERT INTO users (id, email, name, provider, tenant_id, api_key, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                user_id, email, name, provider, tenant_id, api_key, now, now,
            )
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return dict(row) if row else {}


async def get_user_by_id(user_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    return dict(row) if row else None


async def get_user_by_email(email: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE email = $1", email)
    return dict(row) if row else None


async def get_user_by_api_key(api_key: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE api_key = $1", api_key)
    return dict(row) if row else None


# ── Email Logs ──────────────────────────────────────────────

async def log_email_sent(recipient_email: str, recipient_name: str, tenant_id: str, invitation_link: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO email_logs (recipient_email, recipient_name, tenant_id, invitation_link, status) VALUES ($1,$2,$3,$4,'sent') RETURNING id",
            recipient_email, recipient_name, tenant_id, invitation_link,
        )
    return row["id"]


async def log_email_failed(recipient_email: str, error_message: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO email_logs (recipient_email, status, error_message) VALUES ($1,'failed',$2) RETURNING id",
            recipient_email, error_message,
        )
    return row["id"]


async def update_email_event(recipient_email: str, event_type: str) -> None:
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        if event_type == 'open':
            await conn.execute(
                "UPDATE email_logs SET opened_at=$1, status='opened' WHERE recipient_email=$2 AND opened_at IS NULL",
                now, recipient_email,
            )
        elif event_type == 'click':
            await conn.execute(
                "UPDATE email_logs SET clicked_at=$1, status='clicked' WHERE recipient_email=$2 AND clicked_at IS NULL",
                now, recipient_email,
            )
        elif event_type == 'delivered':
            await conn.execute(
                "UPDATE email_logs SET delivered_at=$1 WHERE recipient_email=$2 AND delivered_at IS NULL",
                now, recipient_email,
            )
        elif event_type == 'bounce':
            await conn.execute(
                "UPDATE email_logs SET status='bounced' WHERE recipient_email=$1", recipient_email,
            )


async def get_email_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM email_logs")
        sent = await conn.fetchval("SELECT COUNT(*) FROM email_logs WHERE status != 'pending'")
        opened = await conn.fetchval("SELECT COUNT(*) FROM email_logs WHERE opened_at IS NOT NULL")
        clicked = await conn.fetchval("SELECT COUNT(*) FROM email_logs WHERE clicked_at IS NOT NULL")
        bounced = await conn.fetchval("SELECT COUNT(*) FROM email_logs WHERE status = 'bounced'")
    return {
        "total": total, "sent": sent, "opened": opened, "clicked": clicked,
        "bounced": bounced,
        "open_rate": round(opened / sent * 100, 1) if sent else 0,
        "click_rate": round(clicked / sent * 100, 1) if sent else 0,
    }


# ── User Events ─────────────────────────────────────────────

async def log_user_event(tenant_id: str, event_type: str, user_id: str = "",
                         event_data: dict = None, ip_address: str = "", user_agent: str = "") -> int:
    pool = await get_pool()
    data_json = json.dumps(event_data or {})
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO user_events (tenant_id, user_id, event_type, event_data, ip_address, user_agent) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            tenant_id, user_id, event_type, data_json, ip_address, user_agent,
        )
    return row["id"]


async def get_analytics_overview(days: int = 30) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(DISTINCT tenant_id) FROM user_events")
        active_today = await conn.fetchval("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE created_at >= now() - interval '1 day'")
        active_week = await conn.fetchval("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE created_at >= now() - interval '7 days'")
        active_month = await conn.fetchval("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE created_at >= now() - interval '30 days'")

        login_users = await conn.fetchval("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE event_type='login'")
        submit_users = await conn.fetchval("SELECT COUNT(DISTINCT tenant_id) FROM user_events WHERE event_type='job_submit'")
        login_to_submit = round(submit_users / login_users, 2) if login_users else 0
        total_submits = await conn.fetchval("SELECT COUNT(*) FROM user_events WHERE event_type='job_submit'")
        total_completes = await conn.fetchval("SELECT COUNT(*) FROM user_events WHERE event_type='job_complete'")
        submit_to_complete = round(total_completes / total_submits, 2) if total_submits else 0

        total_jobs = await conn.fetchval("SELECT COUNT(*) FROM user_events WHERE event_type IN ('job_submit', 'job_complete', 'job_failed')")

        rows = await conn.fetch(
            "SELECT date(created_at) as d, event_type, COUNT(*) as cnt FROM user_events "
            "WHERE created_at >= now() - $1::interval GROUP BY 1, 2 ORDER BY 1",
            f"{days} days",
        )
        dates, logins, submits, completes = [], [], [], []
        for r in rows:
            d = str(r["d"])
            if d not in dates:
                dates.append(d); logins.append(0); submits.append(0); completes.append(0)
            idx = dates.index(d)
            if r["event_type"] == "login": logins[idx] = r["cnt"]
            elif r["event_type"] == "job_submit": submits[idx] = r["cnt"]
            elif r["event_type"] == "job_complete": completes[idx] = r["cnt"]

    return {
        "total_users": total_users, "active_users_today": active_today,
        "active_users_week": active_week, "active_users_month": active_month,
        "conversion": {"login_to_submit": login_to_submit, "submit_to_complete": submit_to_complete},
        "jobs": {"total": total_jobs},
        "events_timeline": {"dates": dates, "logins": logins, "submits": submits, "completes": completes},
    }


async def get_analytics_events(limit: int = 100, offset: int = 0, event_type: str = "",
                                tenant_id: str = "", from_date: str = "", to_date: str = "") -> tuple[list[dict], int]:
    pool = await get_pool()
    where = ["TRUE"]
    params = []
    idx = 1
    if event_type:
        where.append(f"event_type = ${idx}"); params.append(event_type); idx += 1
    if tenant_id:
        where.append(f"tenant_id = ${idx}"); params.append(tenant_id); idx += 1
    if from_date:
        where.append(f"created_at >= ${idx}"); params.append(from_date); idx += 1
    if to_date:
        where.append(f"created_at <= ${idx}"); params.append(to_date); idx += 1
    wh = " AND ".join(where)
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM user_events WHERE {wh}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM user_events WHERE {wh} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
            *params, limit, offset,
        )
    return [dict(r) for r in rows], total


# ── Feedback ────────────────────────────────────────────────

async def save_feedback(tenant_id: str, user_id: str, rating: int, liked: str = "",
                        improvement: str = "", bug: str = "", user_agent: str = "") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO feedback (tenant_id, user_id, rating, liked, improvement, bug, user_agent) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            tenant_id, user_id, rating, liked, improvement, bug, user_agent,
        )
    return row["id"]


async def get_feedback(limit: int = 50, offset: int = 0, from_date: str = "",
                       to_date: str = "", rating: int = 0) -> tuple[list[dict], int]:
    pool = await get_pool()
    where = ["TRUE"]
    params = []
    idx = 1
    if from_date:
        where.append(f"created_at >= ${idx}"); params.append(from_date); idx += 1
    if to_date:
        where.append(f"created_at <= ${idx}"); params.append(to_date); idx += 1
    if rating > 0:
        where.append(f"rating = ${idx}"); params.append(rating); idx += 1
    wh = " AND ".join(where)
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM feedback WHERE {wh}", *params)
        rows = await conn.fetch(
            f"SELECT * FROM feedback WHERE {wh} ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
            *params, limit, offset,
        )
    return [dict(r) for r in rows], total


# ── Workers ─────────────────────────────────────────────────

async def get_tenant_workers(tenant_id: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM workers WHERE tenant_id = $1 ORDER BY created_at DESC", tenant_id)
    return [dict(r) for r in rows]


async def drain_worker(worker_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE workers SET status='draining', updated_at=now() WHERE id=$1", worker_id)
