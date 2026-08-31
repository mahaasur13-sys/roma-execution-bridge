#!/usr/bin/env python3
"""Dual-DB adapter: PostgreSQL when PG_DSN is set, SQLite otherwise.

All functions in this module mirror db.py exactly.
Import this instead of db.py to get automatic PostgreSQL fallback.
"""

import asyncio
import logging
import os
import threading

try:
    import psycopg2
    import psycopg2.pool
    _HAS_PSYCOPG2 = True
except ImportError:
    psycopg2 = None  # type: ignore
    _HAS_PSYCOPG2 = False

logger = logging.getLogger("roma.db")

_USE_PG: bool | None = None
_PG_POOL = None
_PG_POOL_LOCK = threading.Lock()
_PG_POOL_CONFIG = {"minconn": 2, "maxconn": 20, "connect_timeout": 10}


def _pg_enabled() -> bool:
    global _USE_PG
    if _USE_PG is None:
        _USE_PG = bool(os.environ.get("PG_DSN"))
        if _USE_PG:
            logger.info("Using PostgreSQL (PG_DSN=%s)", os.environ["PG_DSN"])
        else:
            logger.info("Using SQLite (PG_DSN not set)")
    return _USE_PG


# ── Sync SQLite imports (used when PG_DSN is empty) ────────


# ── Async runner (handles nested event loops) ──────────

def _run_async(coro):
    """Run async coroutine from sync context — safe inside asyncio.run()."""
    import concurrent.futures
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Running loop exists — delegate to a fresh thread
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()

def _sqlite_conn():
    import sqlite3
    from pathlib import Path
    DB_PATH = Path(__file__).parent / "data" / "roma.db"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c



# ── Public API (mirrors db.py) ──────────────────────────────

def _pg_conn():
    """Get a psycopg2 connection from pool (thread-safe, double-checked locking)."""
    global _PG_POOL
    pg_dsn = os.environ.get("PG_DSN", "")
    if not pg_dsn:
        return None
    if _PG_POOL is None:
        with _PG_POOL_LOCK:
            if _PG_POOL is None:
                # Append keepalive + timeout params to DSN
                _pg_keepalive = (
                    "keepalives=1&keepalives_idle=60&keepalives_interval=10"
                    "&keepalives_count=3&connect_timeout=10"
                )
                _dsn = pg_dsn if "?" in pg_dsn else pg_dsn + "?"
                _dsn = _dsn + "&" + _pg_keepalive if "?" in pg_dsn and "=" in pg_dsn.split("?")[-1] else _dsn + _pg_keepalive
                _PG_POOL = psycopg2.pool.ThreadedConnectionPool(
                    _PG_POOL_CONFIG["minconn"], _PG_POOL_CONFIG["maxconn"], _dsn
                )
                import re as _re; _masked = _re.sub(r":[^:@]\+@", r":***@", pg_dsn)
                logger.info("PG pool created: min=%d max=%d, dsn=%s",
                           _PG_POOL_CONFIG["minconn"], _PG_POOL_CONFIG["maxconn"],
                           _masked)
    try:
        return _PG_POOL.getconn()
    except psycopg2.pool.PoolError:
        logger.error("PG pool exhausted (max=%d)", _PG_POOL_CONFIG["maxconn"])
        raise

def _pg_return(conn, close_on_error=False, error_context=""):
    """Return connection to pool; close broken connections on error."""
    global _PG_POOL
    if not _PG_POOL or not conn:
        return
    try:
        if close_on_error:
            _PG_POOL.putconn(conn, close=True)
        else:
            _PG_POOL.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

def close_pg_pool():
    """Close the pool — call on application shutdown."""
    global _PG_POOL
    if _PG_POOL:
        _PG_POOL.closeall()
        _PG_POOL = None
        logger.info("PG pool closed")

def init_db() -> None:
    if _pg_enabled():
        from db_pg_sync import init_db as pg_init
        conn = _pg_conn()
        try:
            pg_init(conn)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.init_db()


def seed_tenants(api_keys: dict) -> None:
    if _pg_enabled():
        from db_pg_sync import seed_tenants as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, api_keys)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.seed_tenants(api_keys)


def get_tenant(tenant_id: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import get_tenant as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, tenant_id)
        finally:
            _pg_return(conn)
    import db
    return db.get_tenant(tenant_id)


def update_tenant_subscription(tenant_id: str, stripe_customer_id: str = "",
                               stripe_subscription_id: str = "", subscription_status: str = "",
                               plan: str = "", subscription_end_date: str | None = None) -> None:
    if _pg_enabled():
        from db_pg_sync import update_tenant_subscription as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, tenant_id, stripe_customer_id, stripe_subscription_id,
                  subscription_status, plan, subscription_end_date)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.update_tenant_subscription(tenant_id, stripe_customer_id, stripe_subscription_id,
                                      subscription_status, plan, subscription_end_date)


def set_tenant_inactive(tenant_id: str) -> None:
    if _pg_enabled():
        from db_pg_sync import set_tenant_inactive as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, tenant_id)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.set_tenant_inactive(tenant_id)


def record_webhook_event(stripe_event_id: str, event_type: str, tenant_id: str, payload: str) -> None:
    if _pg_enabled():
        from db_pg_sync import record_webhook_event as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, stripe_event_id, event_type, tenant_id, payload)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.record_webhook_event(stripe_event_id, event_type, tenant_id, payload)


def list_webhook_events(limit: int = 20) -> list[dict]:
    if _pg_enabled():
        from db_pg_sync import list_webhook_events as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, limit)
        finally:
            _pg_return(conn)
    import db
    return db.list_webhook_events(limit)


def is_invoice_processed(invoice_id: str) -> bool:
    if _pg_enabled():
        from db_pg_sync import is_invoice_processed as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, invoice_id)
        finally:
            _pg_return(conn)
    import db
    return db.is_invoice_processed(invoice_id)


def mark_invoice_processed(invoice_id: str, event_type: str = "", tenant_id: str = "") -> None:
    if _pg_enabled():
        from db_pg_sync import mark_invoice_processed as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, invoice_id, event_type, tenant_id)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.mark_invoice_processed(invoice_id, event_type, tenant_id)


def add_lead(email: str, company: str = "", role: str = "", use_case: str = "", source: str = "") -> int:
    if _pg_enabled():
        from db_pg_sync import add_lead as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, email, company, role, use_case, source)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    import db
    return db.add_lead(email, company, role, use_case, source)


def list_leads(status: str = "") -> list[dict]:
    if _pg_enabled():
        from db_pg_sync import list_leads as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, status)
        finally:
            _pg_return(conn)
    import db
    return db.list_leads(status)


def update_lead_status(lead_id: int, status: str, notes: str = "") -> None:
    if _pg_enabled():
        from db_pg_sync import update_lead_status as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, lead_id, status, notes)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.update_lead_status(lead_id, status, notes)


def upsert_oauth_user(user_id: str, email: str, name: str, provider: str, tenant_id: str, api_key: str) -> dict:
    if _pg_enabled():
        from db_pg_sync import upsert_oauth_user as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, user_id, email, name, provider, tenant_id, api_key)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    import db
    return db.upsert_oauth_user(user_id, email, name, provider, tenant_id, api_key)


def get_user_by_id(user_id: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import get_user_by_id as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, user_id)
        finally:
            _pg_return(conn)
    import db
    return db.get_user_by_id(user_id)


def get_user_by_email(email: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import get_user_by_email as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, email)
        finally:
            _pg_return(conn)
    import db
    return db.get_user_by_email(email)


def get_user_by_api_key(api_key: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import get_user_by_api_key as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, api_key)
        finally:
            _pg_return(conn)
    import db
    return db.get_user_by_api_key(api_key)


def log_email_sent(recipient_email: str, recipient_name: str, tenant_id: str, invitation_link: str) -> int:
    if _pg_enabled():
        from db_pg_sync import log_email_sent as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, recipient_email, recipient_name, tenant_id, invitation_link)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    import db
    return db.log_email_sent(recipient_email, recipient_name, tenant_id, invitation_link)


def log_email_failed(recipient_email: str, error_message: str) -> int:
    if _pg_enabled():
        from db_pg_sync import log_email_failed as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, recipient_email, error_message)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    import db
    return db.log_email_failed(recipient_email, error_message)


def update_email_event(recipient_email: str, event_type: str) -> None:
    if _pg_enabled():
        from db_pg_sync import update_email_event as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, recipient_email, event_type)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.update_email_event(recipient_email, event_type)


def get_email_stats() -> dict:
    if _pg_enabled():
        from db_pg_sync import get_email_stats as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn)
        finally:
            _pg_return(conn)
    import db
    return db.get_email_stats()


def log_user_event(tenant_id: str, event_type: str, user_id: str = "",
                   event_data: dict = None, ip_address: str = "", user_agent: str = "") -> int:
    if _pg_enabled():
        from db_pg_sync import log_user_event as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, tenant_id, event_type, user_id, event_data, ip_address, user_agent)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    import db
    return db.log_user_event(tenant_id, event_type, user_id, event_data, ip_address, user_agent)


def get_analytics_overview(days: int = 30) -> dict:
    if _pg_enabled():
        from db_pg_sync import get_analytics_overview as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, days)
        finally:
            _pg_return(conn)
    import db
    return db.get_analytics_overview(days)


def get_analytics_users(start_date: str = "", end_date: str = "", sort_by: str = "last_seen") -> list[dict]:
    import db
    return db.get_analytics_users(start_date, end_date, sort_by)


def get_analytics_events(limit: int = 100, offset: int = 0, event_type: str = "",
                          tenant_id: str = "", from_date: str = "", to_date: str = "") -> tuple[list[dict], int]:
    if _pg_enabled():
        from db_pg_sync import get_analytics_events as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, limit, offset, event_type, tenant_id, from_date, to_date)
        finally:
            _pg_return(conn)
    import db
    return db.get_analytics_events(limit, offset, event_type, tenant_id, from_date, to_date)


def save_feedback(tenant_id: str, user_id: str, rating: int, liked: str = "",
                  improvement: str = "", bug: str = "", user_agent: str = "") -> int:
    if _pg_enabled():
        from db_pg_sync import save_feedback as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, tenant_id, user_id, rating, liked, improvement, bug, user_agent)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    import db
    return db.save_feedback(tenant_id, user_id, rating, liked, improvement, bug, user_agent)


def get_feedback(limit: int = 50, offset: int = 0, from_date: str = "",
                 to_date: str = "", rating: int = 0) -> tuple[list[dict], int]:
    if _pg_enabled():
        from db_pg_sync import get_feedback as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, limit, offset, from_date, to_date, rating)
        finally:
            _pg_return(conn)
    import db
    return db.get_feedback(limit, offset, from_date, to_date, rating)


def get_tenant_workers(tenant_id: str) -> list[dict]:
    if _pg_enabled():
        from db_pg_sync import get_tenant_workers as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, tenant_id)
        finally:
            _pg_return(conn)
    import db
    return db.get_tenant_workers(tenant_id)


def drain_worker(worker_id: str) -> None:
    if _pg_enabled():
        from db_pg_sync import drain_worker as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, worker_id)
            conn.commit()
        finally:
            _pg_return(conn)
    else:
        import db
        db.drain_worker(worker_id)


def register_worker(worker_id: str, tenant_id: str, capabilities: dict = None) -> None:
    if _pg_enabled():
        from db_pg_sync import register_worker as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, worker_id, tenant_id, capabilities)
            conn.commit()
        finally:
            _pg_return(conn)
    # SQLite path has no workers table — no-op.


def update_worker_heartbeat(worker_id: str) -> None:
    if _pg_enabled():
        from db_pg_sync import update_worker_heartbeat as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, worker_id)
            conn.commit()
        finally:
            _pg_return(conn)


def release_worker(worker_id: str) -> None:
    if _pg_enabled():
        from db_pg_sync import release_worker as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, worker_id)
            conn.commit()
        finally:
            _pg_return(conn)


def get_daily_stats(days: int = 7) -> dict:
    """Daily job count + GPU hours (last 7 days) for /stats/daily."""
    if _pg_enabled():
        from db_pg_sync import get_daily_stats as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn)
        finally:
            _pg_return(conn)
    return {"dates": [], "jobs_count": [], "gpu_hours": []}


def _ensure_submit_idempotency_table(c) -> None:
    c.execute("""
        CREATE TABLE IF NOT EXISTS submit_idempotency_keys (
            tenant_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            job_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (tenant_id, idempotency_key)
        )
    """)


def _ensure_execution_jobs_table(c) -> None:
    """Create execution_jobs in SQLite if absent and backfill the execution-billing
    columns (backend, backend_job_id, ...) on tables created before migration 005.

    Mirrors migrations/002_billing_pg.sql + migrations/005_execution_billing.sql.
    backend is intentionally nullable (no silent 'local' default on an empty chain).
    """
    c.execute("""
        CREATE TABLE IF NOT EXISTS execution_jobs (
            id              TEXT PRIMARY KEY,
            decision_id     TEXT,
            tenant_id       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            payload         TEXT DEFAULT '{}',
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now')),
            cost_usd        REAL NOT NULL DEFAULT 0.0,
            backend         TEXT,
            backend_job_id  TEXT,
            duration_seconds REAL DEFAULT 0.0,
            started_at      TEXT,
            completed_at    TEXT,
            error           TEXT
        )
    """)
    existing = {row[1] for row in c.execute("PRAGMA table_info(execution_jobs)").fetchall()}
    for column, ddl in (
        ("cost_usd", "REAL NOT NULL DEFAULT 0.0"),
        ("backend", "TEXT"),
        ("backend_job_id", "TEXT"),
        ("duration_seconds", "REAL DEFAULT 0.0"),
        ("started_at", "TEXT"),
        ("completed_at", "TEXT"),
        ("error", "TEXT"),
    ):
        if column not in existing:
            c.execute(f"ALTER TABLE execution_jobs ADD COLUMN {column} {ddl}")


def find_job_by_idempotency(tenant_id: str, idempotency_key: str):
    """Return job_id for (tenant_id, idempotency_key), or None if not present."""
    if _pg_enabled():
        from db_pg_sync import find_job_by_idempotency as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, tenant_id, idempotency_key)
        finally:
            _pg_return(conn)
    c = _sqlite_conn()
    try:
        _ensure_submit_idempotency_table(c)
        row = c.execute(
            "SELECT job_id FROM submit_idempotency_keys WHERE tenant_id=? AND idempotency_key=?",
            (tenant_id, idempotency_key),
        ).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def create_job_idempotency(tenant_id: str, idempotency_key: str, job_id: str) -> bool:
    """Reserve (tenant_id, idempotency_key) -> job_id atomically.

    Returns True if this call created the mapping, False if the key already
    existed (another identical submit won the race).
    """
    if _pg_enabled():
        from db_pg_sync import create_job_idempotency as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, tenant_id, idempotency_key, job_id)
        finally:
            _pg_return(conn)
    c = _sqlite_conn()
    try:
        _ensure_submit_idempotency_table(c)
        c.execute(
            "INSERT OR IGNORE INTO submit_idempotency_keys (tenant_id, idempotency_key, job_id) VALUES (?,?,?)",
            (tenant_id, idempotency_key, job_id),
        )
        c.commit()
        return c.rowcount > 0
    finally:
        c.close()


def list_tenants() -> list[dict]:
    if _pg_enabled():
        from db_pg_sync import list_tenants as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn)
        finally:
            _pg_return(conn)
    import db
    return db.list_tenants()

# ── Async PostgreSQL delegates ──────────────────────────────

async def _init_pg():
    from db_pg import init_db as pg_init
    await pg_init()


async def _seed_tenants_pg(api_keys):
    from db_pg import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        for key, info in api_keys.items():
            await conn.execute(
                "INSERT INTO tenants (id, api_key, name, plan, subscription_status) VALUES ($1,$2,$3,'free','inactive') ON CONFLICT (id) DO NOTHING",
                info.get("tenant_id", ""), key, info.get("name", info.get("tenant_id", "")),
            )


async def _get_tenant_pg(tenant_id):
    from db_pg import get_tenant as pg_fn
    return await pg_fn(tenant_id)


async def _update_tenant_subscription_pg(*args):
    from db_pg import update_tenant_subscription as pg_fn
    await pg_fn(*args)


async def _set_tenant_inactive_pg(tenant_id):
    from db_pg import set_tenant_inactive as pg_fn
    await pg_fn(tenant_id)


async def _record_webhook_event_pg(*args):
    from db_pg import record_webhook_event as pg_fn
    await pg_fn(*args)


async def _list_webhook_events_pg(limit):
    from db_pg import list_webhook_events as pg_fn
    return await pg_fn(limit)


async def _is_invoice_processed_pg(invoice_id):
    from db_pg import is_invoice_processed as pg_fn
    return await pg_fn(invoice_id)


async def _mark_invoice_processed_pg(*args):
    from db_pg import mark_invoice_processed as pg_fn
    await pg_fn(*args)


async def _add_lead_pg(*args):
    from db_pg import add_lead as pg_fn
    return await pg_fn(*args)


async def _list_leads_pg(status):
    from db_pg import list_leads as pg_fn
    return await pg_fn(status)


async def _update_lead_status_pg(*args):
    from db_pg import update_lead_status as pg_fn
    await pg_fn(*args)


async def _upsert_oauth_user_pg(*args):
    from db_pg import upsert_oauth_user as pg_fn
    return await pg_fn(*args)


async def _get_user_by_id_pg(user_id):
    from db_pg import get_user_by_id as pg_fn
    return await pg_fn(user_id)


async def _get_user_by_email_pg(email):
    from db_pg import get_user_by_email as pg_fn
    return await pg_fn(email)


async def _get_user_by_api_key_pg(api_key):
    from db_pg import get_user_by_api_key as pg_fn
    return await pg_fn(api_key)


async def _log_email_sent_pg(*args):
    from db_pg import log_email_sent as pg_fn
    return await pg_fn(*args)


async def _log_email_failed_pg(*args):
    from db_pg import log_email_failed as pg_fn
    return await pg_fn(*args)


async def _update_email_event_pg(*args):
    from db_pg import update_email_event as pg_fn
    await pg_fn(*args)


async def _get_email_stats_pg():
    from db_pg import get_email_stats as pg_fn
    return await pg_fn()


async def _log_user_event_pg(*args):
    from db_pg import log_user_event as pg_fn
    return await pg_fn(*args)


async def _get_analytics_overview_pg(days):
    from db_pg import get_analytics_overview as pg_fn
    return await pg_fn(days)


async def _get_analytics_events_pg(*args):
    from db_pg import get_analytics_events as pg_fn
    return await pg_fn(*args)


async def _save_feedback_pg(*args):
    from db_pg import save_feedback as pg_fn
    return await pg_fn(*args)


async def _get_feedback_pg(*args):
    from db_pg import get_feedback as pg_fn
    return await pg_fn(*args)


async def _get_tenant_workers_pg(tenant_id):
    from db_pg import get_tenant_workers as pg_fn
    return await pg_fn(tenant_id)


async def _drain_worker_pg(worker_id):
    from db_pg import drain_worker as pg_fn
    await pg_fn(worker_id)


async def _list_tenants_pg():
    from db_pg import list_tenants as pg_fn
    return await pg_fn()


def get_worker_by_id(worker_id: str) -> dict | None:
    """Fetch a single worker by ID. Supports PostgreSQL + SQLite."""
    if _pg_enabled():
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM workers WHERE id = %s", (worker_id,))
                row = cur.fetchone()
                if row:
                    return dict(zip([d[0] for d in cur.description], row))
                return None
        finally:
            _pg_return(conn)
    c = _sqlite_conn()
    try:
        row = c.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()

# ────────────────────────────────────────────────
# DecisionOS Week 1 — jobs, decisions, usage, audit
# ────────────────────────────────────────────────

def insert_job(job_id: str, tenant_id: str, status: str, decision_id: str, payload: dict) -> dict:
    import json
    if _pg_enabled():
        return _run_async(_insert_job_pg(job_id, tenant_id, status, decision_id, json.dumps(payload)))
    return _insert_job_sqlite(job_id, tenant_id, status, decision_id, json.dumps(payload))

async def _insert_job_pg(job_id, tenant_id, status, decision_id, payload_json):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO execution_jobs (id, decision_id, tenant_id, status, payload)"
                " VALUES (%s, %s, %s, %s, %s::jsonb)",
                (job_id, decision_id, tenant_id, status, payload_json),
            )
        conn.commit()
        return {"id": job_id, "tenant_id": tenant_id, "status": status, "decision_id": decision_id}
    finally:
        _pg_return(conn)

def _insert_job_sqlite(job_id, tenant_id, status, decision_id, payload_json):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, decision_id, tenant_id, status, payload) VALUES (?,?,?,?,?)",
            (job_id, decision_id, tenant_id, status, payload_json),
        )
        c.commit()
        return {"id": job_id, "tenant_id": tenant_id, "status": status, "decision_id": decision_id}
    finally:
        c.close()


def insert_job_raw(job_id: str, tenant_id: str, status: str, payload: dict) -> dict:
    import json
    if _pg_enabled():
        return _run_async(_insert_job_raw_pg(job_id, tenant_id, status, json.dumps(payload)))
    return _insert_job_raw_sqlite(job_id, tenant_id, status, json.dumps(payload))

async def _insert_job_raw_pg(job_id, tenant_id, status, payload_json):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO execution_jobs (id, tenant_id, status, payload) VALUES (%s,%s,%s,%s::jsonb)",
                (job_id, tenant_id, status, payload_json),
            )
        conn.commit()
        return {"id": job_id, "tenant_id": tenant_id, "status": status}
    finally:
        _pg_return(conn)

def _insert_job_raw_sqlite(job_id, tenant_id, status, payload_json):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        c.execute("INSERT INTO execution_jobs (id, tenant_id, status, payload) VALUES (?,?,?,?)",
                  (job_id, tenant_id, status, payload_json))
        c.commit()
        return {"id": job_id, "tenant_id": tenant_id, "status": status}
    finally:
        c.close()


def get_job(job_id: str, tenant_id: str) -> dict | None:
    if _pg_enabled():
        return _run_async(_get_job_pg(job_id, tenant_id))
    return _get_job_sqlite(job_id, tenant_id)

async def _get_job_pg(job_id, tenant_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM execution_jobs WHERE id=%s AND tenant_id=%s", (job_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            d = dict(zip(cols, row))
            for dt_col in ("created_at", "started_at", "completed_at"):
                if d.get(dt_col):
                    d[dt_col] = d[dt_col].isoformat() if hasattr(d[dt_col], "isoformat") else str(d[dt_col])
            return d
    finally:
        _pg_return(conn)

def _get_job_sqlite(job_id, tenant_id):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        row = c.execute("SELECT * FROM execution_jobs WHERE id=? AND tenant_id=?", (job_id, tenant_id)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def update_job_status(job_id: str, status: str, completed_at: str | None = None, error: str | None = None):
    if _pg_enabled():
        return _run_async(_update_job_status_pg(job_id, status, completed_at, error))
    return _update_job_status_sqlite(job_id, status, completed_at, error)

async def _update_job_status_pg(job_id, status, completed_at, error):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            sets = ["status = %s"]
            params = [status]
            if status == "running":
                sets.append("started_at = now()")
            if completed_at:
                sets.append("completed_at = %s")
                params.append(completed_at)
            if error is not None:
                sets.append("error = %s")
                params.append(error)
            params.append(job_id)
            cur.execute(f"UPDATE execution_jobs SET {', '.join(sets)} WHERE id = %s", params)
        conn.commit()
    finally:
        _pg_return(conn)

def _update_job_status_sqlite(job_id, status, completed_at, error):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        sets = "status = ?"
        params = [status]
        if status == "running":
            sets += ", started_at = datetime('now')"
        if completed_at:
            sets += ", completed_at = ?"
            params.append(completed_at)
        if error is not None:
            sets += ", error = ?"
            params.append(error)
        params.append(job_id)
        c.execute(f"UPDATE execution_jobs SET {sets} WHERE id = ?", params)
        c.commit()
    finally:
        c.close()


def list_jobs(tenant_id: str, limit: int = 100) -> list[dict]:
    if _pg_enabled():
        return _run_async(_list_jobs_pg(tenant_id, limit))
    return _list_jobs_sqlite(tenant_id, limit)

async def _list_jobs_pg(tenant_id, limit):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM execution_jobs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
                (tenant_id, limit),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        _pg_return(conn)

def _list_jobs_sqlite(tenant_id, limit):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        rows = c.execute(
            "SELECT * FROM execution_jobs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def count_jobs_by_tenant(tenant_id: str) -> int:
    if _pg_enabled():
        return _run_async(_count_jobs_by_tenant_pg(tenant_id))
    return _count_jobs_by_tenant_sqlite(tenant_id)

async def _count_jobs_by_tenant_pg(tenant_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE tenant_id=%s",
                (tenant_id,),
            )
            return cur.fetchone()[0]
    finally:
        _pg_return(conn)

def _count_jobs_by_tenant_sqlite(tenant_id):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        row = c.execute("SELECT COUNT(*) FROM execution_jobs WHERE tenant_id=?", (tenant_id,)).fetchone()
        return row[0] if row else 0
    finally:
        c.close()


def insert_decision_record(decision_id, request_id, tenant_id, result, reason,
                           quota_remaining, estimated_cost, policy_profile):
    if _pg_enabled():
        return _run_async(_insert_decision_record_pg(
            decision_id, request_id, tenant_id, result, reason,
            quota_remaining, estimated_cost, policy_profile))
    return _insert_decision_record_sqlite(
        decision_id, request_id, tenant_id, result, reason,
        quota_remaining, estimated_cost, policy_profile)

async def _insert_decision_record_pg(did, rid, tid, result, reason, quota, cost, policy):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO decision_records (id, request_id, tenant_id, gate_result, gate_reason,"
                " quota_remaining, estimated_cost, policy_profile)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (did, rid, tid, result, reason, quota, cost, policy),
            )
        conn.commit()
        return {"id": did, "result": result}
    finally:
        _pg_return(conn)

def _insert_decision_record_sqlite(did, rid, tid, result, reason, quota, cost, policy):
    c = _sqlite_conn()
    try:
        c.execute(
            "INSERT INTO decision_records (id, request_id, tenant_id, gate_result, gate_reason,"
            " quota_remaining, estimated_cost, policy_profile) VALUES (?,?,?,?,?,?,?,?)",
            (did, rid, tid, result, reason, quota, cost, policy),
        )
        c.commit()
        return {"id": did, "result": result}
    finally:
        c.close()


def insert_decision_request(request_id, tenant_id, request_type, payload, idempotency_key):
    import json
    if _pg_enabled():
        return _run_async(_insert_decision_request_pg(
            request_id, tenant_id, request_type, json.dumps(payload), idempotency_key))
    return _insert_decision_request_sqlite(
        request_id, tenant_id, request_type, json.dumps(payload), idempotency_key)

async def _insert_decision_request_pg(rid, tid, rtype, payload_json, idemp_key):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO decision_requests (id, tenant_id, request_type, payload, idempotency_key)"
                " VALUES (%s,%s,%s,%s::jsonb,%s)",
                (rid, tid, rtype, payload_json, idemp_key),
            )
        conn.commit()
        return {"id": rid}
    finally:
        _pg_return(conn)

def _insert_decision_request_sqlite(rid, tid, rtype, payload_json, idemp_key):
    c = _sqlite_conn()
    try:
        c.execute(
            "INSERT INTO decision_requests (id, tenant_id, request_type, payload, idempotency_key)"
            " VALUES (?,?,?,?,?)",
            (rid, tid, rtype, payload_json, idemp_key),
        )
        c.commit()
        return {"id": rid}
    finally:
        c.close()


def find_decision_by_idempotency(tenant_id: str, idempotency_key: str) -> dict | None:
    if _pg_enabled():
        return _run_async(_find_decision_by_idempotency_pg(tenant_id, idempotency_key))
    return _find_decision_by_idempotency_sqlite(tenant_id, idempotency_key)

async def _find_decision_by_idempotency_pg(tenant_id, idemp_key):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dr.* FROM decision_records dr"
                " JOIN decision_requests dreq ON dr.request_id = dreq.id"
                " WHERE dreq.tenant_id=%s AND dreq.idempotency_key=%s"
                " ORDER BY dr.decided_at DESC LIMIT 1",
                (tenant_id, idemp_key),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
    finally:
        _pg_return(conn)

def _find_decision_by_idempotency_sqlite(tenant_id, idemp_key):
    c = _sqlite_conn()
    try:
        row = c.execute(
            "SELECT dr.* FROM decision_records dr"
            " JOIN decision_requests dreq ON dr.request_id = dreq.id"
            " WHERE dreq.tenant_id=? AND dreq.idempotency_key=?"
            " ORDER BY dr.decided_at DESC LIMIT 1",
            (tenant_id, idemp_key),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def insert_audit_event(event_id, tenant_id, event_type, entity_type, entity_id, data):
    import json
    if _pg_enabled():
        return _run_async(_insert_audit_event_pg(
            event_id, tenant_id, event_type, entity_type, entity_id, json.dumps(data)))
    return _insert_audit_event_sqlite(
        event_id, tenant_id, event_type, entity_type, entity_id, json.dumps(data))

async def _insert_audit_event_pg(eid, tid, etype, ent_type, ent_id, data_json):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_events (id, tenant_id, event_type, entity_type, entity_id, data)"
                " VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
                (eid, tid, etype, ent_type, ent_id, data_json),
            )
        conn.commit()
        return {"id": eid}
    finally:
        _pg_return(conn)

def _insert_audit_event_sqlite(eid, tid, etype, ent_type, ent_id, data_json):
    c = _sqlite_conn()
    try:
        c.execute(
            "INSERT INTO audit_events (id, tenant_id, event_type, entity_type, entity_id, data)"
            " VALUES (?,?,?,?,?,?)",
            (eid, tid, etype, ent_type, ent_id, data_json),
        )
        c.commit()
        return {"id": eid}
    finally:
        c.close()


def get_tenant_usage_db(tenant_id: str) -> dict:
    if _pg_enabled():
        return _run_async(_get_tenant_usage_db_pg(tenant_id))
    return _get_tenant_usage_db_sqlite(tenant_id)

async def _get_tenant_usage_db_pg(tenant_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(gpu_seconds),0), COUNT(*) FROM usage_events WHERE tenant_id=%s",
                (tenant_id,),
            )
            row = cur.fetchone()
            return {"total_gpu_seconds": int(row[0]), "total_jobs": row[1]} if row else {"total_gpu_seconds": 0, "total_jobs": 0}
    finally:
        _pg_return(conn)

def _get_tenant_usage_db_sqlite(tenant_id):
    c = _sqlite_conn()
    try:
        row = c.execute(
            "SELECT COALESCE(SUM(gpu_seconds),0), COUNT(*) FROM usage_events WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        return {"total_gpu_seconds": int(row[0]), "total_jobs": row[1]} if row else {"total_gpu_seconds": 0, "total_jobs": 0}
    finally:
        c.close()


def insert_usage_event(event_id, tenant_id, job_id, gpu_seconds, cost):
    if _pg_enabled():
        return _run_async(_insert_usage_event_pg(event_id, tenant_id, job_id, gpu_seconds, cost))
    return _insert_usage_event_sqlite(event_id, tenant_id, job_id, gpu_seconds, cost)

async def _insert_usage_event_pg(eid, tid, jid, gpu_sec, cost):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usage_events (id, tenant_id, job_id, gpu_seconds, cost)"
                " VALUES (%s,%s,%s,%s,%s)",
                (eid, tid, jid, gpu_sec, cost),
            )
        conn.commit()
        return {"id": eid}
    finally:
        _pg_return(conn)

def _insert_usage_event_sqlite(eid, tid, jid, gpu_sec, cost):
    c = _sqlite_conn()
    try:
        c.execute(
            "INSERT INTO usage_events (id, tenant_id, job_id, gpu_seconds, cost)"
            " VALUES (?,?,?,?,?)",
            (eid, tid, jid, gpu_sec, cost),
        )
        c.commit()
        return {"id": eid}
    finally:
        c.close()


def insert_tenant(tenant_id: str, name: str, api_key_hash: str, tier: str = "start") -> dict:
    if _pg_enabled():
        return _run_async(_insert_tenant_pg(tenant_id, name, api_key_hash, tier))
    return _insert_tenant_sqlite(tenant_id, name, api_key_hash, tier)

async def _insert_tenant_pg(tenant_id, name, api_key_hash, tier):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (id, name, api_key_hash, tier)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, tier=EXCLUDED.tier",
                (tenant_id, name, api_key_hash, tier),
            )
        conn.commit()
        return {"id": tenant_id}
    finally:
        _pg_return(conn)

def _insert_tenant_sqlite(tenant_id, name, api_key_hash, tier):
    c = _sqlite_conn()
    try:
        c.execute(
            "INSERT OR REPLACE INTO tenants (id, name, api_key_hash, tier) VALUES (?,?,?,?)",
            (tenant_id, name, api_key_hash, tier),
        )
        c.commit()
        return {"id": tenant_id}
    finally:
        c.close()


def migrate_all_tenants_to_pg(api_keys: dict):
    """One-shot: copy tenants from api_keys dict into PG."""
    for tenant_id, key_data in api_keys.items():
        if isinstance(key_data, dict):
            name = key_data.get("account_name", tenant_id)
            tier = key_data.get("plan", "start")
            api_key_hash = f"hash_{tenant_id}"
            insert_tenant(tenant_id, name, api_key_hash, tier)
            logger.info("Migrated tenant: %s", tenant_id)
    logger.info("Tenant migration complete: %d tenants", len(api_keys))

def is_pg_connected() -> bool:
    """Check if PostgreSQL connection pool is alive."""
    return _pg_enabled() and _PG_POOL is not None


# ═══════════════════════════════════════════════════════════
# Week 2 — decision/job store + plans loader (extends Week 1)
# ═══════════════════════════════════════════════════════════

def count_jobs_for_tenant(tenant_id: str) -> int:
    """Count total jobs for tenant (for quota gating)."""
    if _pg_enabled():
        return _run_async(_count_jobs_for_tenant_pg(tenant_id))
    return _count_jobs_for_tenant_sqlite(tenant_id)

async def _count_jobs_for_tenant_pg(tenant_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM execution_jobs WHERE tenant_id=%s", (tenant_id,))
            return cur.fetchone()[0]
    finally:
        _pg_return(conn)

def _count_jobs_for_tenant_sqlite(tenant_id):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        row = c.execute("SELECT COUNT(*) FROM execution_jobs WHERE tenant_id=?", (tenant_id,)).fetchone()
        return row[0] if row else 0
    finally:
        c.close()


def insert_execution_job(job_id: str, decision_id: str, tenant_id: str,
                         status: str, payload: dict) -> dict:
    import json
    if _pg_enabled():
        return _run_async(_insert_execution_job_pg(job_id, decision_id, tenant_id, status, json.dumps(payload)))
    return _insert_execution_job_sqlite(job_id, decision_id, tenant_id, status, json.dumps(payload))

async def _insert_execution_job_pg(job_id, decision_id, tenant_id, status, payload_json):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO execution_jobs (id, decision_id, tenant_id, status, payload)"
                " VALUES (%s,%s,%s,%s,%s::jsonb)",
                (job_id, decision_id, tenant_id, status, payload_json),
            )
        conn.commit()
        return {"id": job_id, "tenant_id": tenant_id, "status": status, "decision_id": decision_id}
    finally:
        _pg_return(conn)

def _insert_execution_job_sqlite(job_id, decision_id, tenant_id, status, payload_json):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, decision_id, tenant_id, status, payload) VALUES (?,?,?,?,?)",
            (job_id, decision_id, tenant_id, status, payload_json),
        )
        c.commit()
        return {"id": job_id, "tenant_id": tenant_id, "status": status, "decision_id": decision_id}
    finally:
        c.close()


def get_execution_job(job_id: str) -> dict | None:
    if _pg_enabled():
        return _run_async(_get_execution_job_pg(job_id))
    return _get_execution_job_sqlite(job_id)

async def _get_execution_job_pg(job_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM execution_jobs WHERE id=%s", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            d = dict(zip(cols, row))
            for dt_col in ("created_at", "started_at", "completed_at"):
                if d.get(dt_col) and hasattr(d.get(dt_col), "isoformat"):
                    d[dt_col] = d[dt_col].isoformat()
            return d
    finally:
        _pg_return(conn)

def _get_execution_job_sqlite(job_id):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        row = c.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()

def get_queued_execution_jobs(limit: int = 10) -> list[dict]:
    """Return queued (not yet running) execution jobs, ordered by created_at ASC."""
    if _pg_enabled():
        try:
            conn = _pg_conn()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM execution_jobs WHERE status='queued' ORDER BY created_at ASC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows] if rows else []
        except Exception:
            return []
        finally:
            _pg_return(conn)
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        rows = c.execute(
            "SELECT * FROM execution_jobs WHERE status='queued' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows] if rows else []
    finally:
        c.close()



def update_execution_job(job_id: str, status: str | None = None,
                         backend: str | None = None, backend_job_id: str | None = None,
                         completed_at: str | None = None, error: str | None = None):
    if _pg_enabled():
        return _run_async(_update_execution_job_pg(job_id, status, completed_at, error, backend, backend_job_id))
    return _update_execution_job_sqlite(job_id, status, completed_at, error, backend, backend_job_id)

async def _update_execution_job_pg(job_id, status, completed_at, error, backend, backend_job_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            sets = []
            params = []
            if status is not None:
                sets.append("status = %s")
                params.append(status)
                if status == "running":
                    sets.append("started_at = now()")
            if completed_at is not None:
                sets.append("completed_at = %s")
                params.append(completed_at)
            if error is not None:
                sets.append("error = %s")
                params.append(error)
            if backend is not None:
                sets.append("backend = %s")
                params.append(backend)
            if backend_job_id is not None:
                sets.append("backend_job_id = %s")
                params.append(backend_job_id)
            if sets:
                params.append(job_id)
                cur.execute(f"UPDATE execution_jobs SET {', '.join(sets)} WHERE id = %s", params)
        conn.commit()
    finally:
        _pg_return(conn)

def _update_execution_job_sqlite(job_id, status, completed_at, error, backend, backend_job_id):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        sets = []
        params = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            if status == "running":
                sets.append("started_at = datetime('now')")
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(completed_at)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if backend is not None:
            sets.append("backend = ?")
            params.append(backend)
        if backend_job_id is not None:
            sets.append("backend_job_id = ?")
            params.append(backend_job_id)
        if sets:
            params.append(job_id)
            c.execute(f"UPDATE execution_jobs SET {', '.join(sets)} WHERE id = ?", params)
        c.commit()
    finally:
        c.close()


def list_tenant_jobs(tenant_id: str, limit: int = 100) -> list[dict]:
    if _pg_enabled():
        return _run_async(_list_tenant_jobs_pg(tenant_id, limit))
    return _list_tenant_jobs_sqlite(tenant_id, limit)

async def _list_tenant_jobs_pg(tenant_id, limit):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM execution_jobs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
                (tenant_id, limit),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        _pg_return(conn)

def _list_tenant_jobs_sqlite(tenant_id, limit):
    c = _sqlite_conn()
    try:
        _ensure_execution_jobs_table(c)
        rows = c.execute(
            "SELECT * FROM execution_jobs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def get_decision_record(decision_id: str) -> dict | None:
    if _pg_enabled():
        return _run_async(_get_decision_record_pg(decision_id))
    return _get_decision_record_sqlite(decision_id)

async def _get_decision_record_pg(decision_id):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM decision_records WHERE id=%s", (decision_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc[0] for desc in cur.description]
            d = dict(zip(cols, row))
            # Cast UUIDs and timestamps
            for k in ("id", "request_id"):
                if d.get(k):
                    d[k] = str(d[k])
            if d.get("decided_at") and hasattr(d["decided_at"], "isoformat"):
                d["decided_at"] = d["decided_at"].isoformat()
            return d
    finally:
        _pg_return(conn)

def _get_decision_record_sqlite(decision_id):
    c = _sqlite_conn()
    try:
        row = c.execute("SELECT * FROM decision_records WHERE id=?", (decision_id,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def list_decision_records(tenant_id: str, result: str | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """Returns (items, total_count)."""
    if _pg_enabled():
        return _run_async(_list_decision_records_pg(tenant_id, result, date_from, date_to, limit, offset))
    return _list_decision_records_sqlite(tenant_id, result, date_from, date_to, limit, offset)

async def _list_decision_records_pg(tenant_id, result, date_from, date_to, limit, offset):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            where = ["tenant_id=%s"]; params = [tenant_id]
            if result:
                where.append("gate_result=%s"); params.append(result)
            if date_from:
                where.append("decided_at >= %s"); params.append(date_from)
            if date_to:
                where.append("decided_at <= %s"); params.append(date_to)
            where_clause = " AND ".join(where)
            cur.execute(
                f"SELECT COUNT(*) FROM decision_records WHERE {where_clause}", params,
            )
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT * FROM decision_records WHERE {where_clause} ORDER BY decided_at DESC LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            cols = [desc[0] for desc in cur.description]
            items = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                for k in ("id", "request_id"):
                    if d.get(k): d[k] = str(d[k])
                if d.get("decided_at") and hasattr(d["decided_at"], "isoformat"):
                    d["decided_at"] = d["decided_at"].isoformat()
                items.append(d)
            return items, total
    finally:
        _pg_return(conn)

def _list_decision_records_sqlite(tenant_id, result, date_from, date_to, limit, offset):
    c = _sqlite_conn()
    try:
        where = ["tenant_id=?"]; params = [tenant_id]
        if result:
            where.append("gate_result=?"); params.append(result)
        if date_from:
            where.append("decided_at >= ?"); params.append(date_from)
        if date_to:
            where.append("decided_at <= ?"); params.append(date_to)
        where_clause = " AND ".join(where)
        total = c.execute(f"SELECT COUNT(*) FROM decision_records WHERE {where_clause}", params).fetchone()[0]
        rows = c.execute(
            f"SELECT * FROM decision_records WHERE {where_clause} ORDER BY decided_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return ([dict(r) for r in rows], total)
    finally:
        c.close()


# ── Plans loader (shared between gate and API) ──

def _load_plans() -> dict:
    """Load pricing plans from plans.json or return sensible defaults."""
    import json
    from pathlib import Path
    plan_path = Path(__file__).parent / "plans.json"
    try:
        if plan_path.exists():
            return json.loads(plan_path.read_text())
    except Exception:
        pass
    # Sensible defaults
    return {
        "free": {"max_jobs": 10, "max_gpu_hours": 0, "name": "Free"},
        "start": {"max_jobs": 50, "max_gpu_hours": 10, "name": "Start"},
        "pro": {"max_jobs": 150, "max_gpu_hours": 50, "name": "Pro"},
        "enterprise": {"max_jobs": -1, "max_gpu_hours": -1, "name": "Enterprise"},
    }


# ────────────────────────────────────────
# DecisionOS Week 2 helpers
# ────────────────────────────────────────

def count_jobs_for_tenant(tenant_id: str) -> int:
    if _pg_enabled():
        async def _count():
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM execution_jobs WHERE tenant_id=%s AND status IN (%s,%s,%s)",
                                (tenant_id, "queued", "running", "pending"))
                    return cur.fetchone()[0]
            finally:
                _pg_return(conn)
        return _run_async(_count())
    else:
        c = _sqlite_conn()
        try:
            _ensure_execution_jobs_table(c)
            row = c.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE tenant_id=? AND status IN (?,?,?)",
                (tenant_id, "queued", "running", "pending"),
            ).fetchone()
            return row[0] if row else 0
        finally:
            c.close()

def _load_plans() -> dict:
    import json
    try:
        with open("plans.json", "r") as f:
            return json.load(f)
    except Exception:
        return {"free": {"max_jobs": 10, "gpu": False}, "pro": {"max_jobs": 150, "gpu": True}}

def get_decision_record(decision_id: str) -> dict | None:
    if _pg_enabled():
        async def _get():
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, request_id, tenant_id, gate_result, gate_reason, quota_remaining, estimated_cost, decided_at FROM decision_records WHERE id=%s",
                        (decision_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        return {"id": row[0], "request_id": row[1], "tenant_id": row[2],
                                "gate_result": row[3], "gate_reason": row[4],
                                "quota_remaining": row[5], "estimated_cost": row[6],
                                "decided_at": row[7].isoformat() if row[7] else None}
                    return None
            finally:
                _pg_return(conn)
        return _run_async(_get())
    else:
        c = _sqlite_conn()
        try:
            row = c.execute(
                "SELECT id, request_id, tenant_id, gate_result, gate_reason, quota_remaining, estimated_cost, decided_at FROM decision_records WHERE id=?",
                (decision_id,),
            ).fetchone()
            return {"id": row[0], "request_id": row[1], "tenant_id": row[2],
                    "gate_result": row[3], "gate_reason": row[4],
                    "quota_remaining": row[5], "estimated_cost": row[6],
                    "decided_at": row[7]} if row else None
        finally:
            c.close()

def list_decision_records(tenant_id: str, result: str | None = None,
                          date_from: str | None = None, date_to: str | None = None,
                          limit: int = 20, offset: int = 0) -> tuple:
    if _pg_enabled():
        async def _list():
            conn = _pg_conn()
            try:
                with conn.cursor() as cur:
                    clauses = ["tenant_id=%s"]
                    params = [tenant_id]
                    if result:
                        clauses.append("gate_result=%s")
                        params.append(result)
                    if date_from:
                        clauses.append("decided_at >= %s")
                        params.append(date_from)
                    if date_to:
                        clauses.append("decided_at <= %s")
                        params.append(date_to)

                    where = " AND ".join(clauses)
                    cur.execute(f"SELECT COUNT(*) FROM decision_records WHERE {where}", params)
                    total = cur.fetchone()[0]
                    cur.execute(
                        f"SELECT id, request_id, gate_result, gate_reason, estimated_cost, quota_remaining, decided_at FROM decision_records WHERE {where} ORDER BY decided_at DESC LIMIT %s OFFSET %s",
                        params + [limit, offset],
                    )
                    rows = [{"id": r[0], "request_id": r[1], "gate_result": r[2],
                             "gate_reason": r[3], "estimated_cost": r[4],
                             "quota_remaining": r[5], "decided_at": r[6].isoformat() if r[6] else None}
                            for r in cur.fetchall()]
                    return rows, total
            finally:
                _pg_return(conn)
        return _run_async(_list())
    else:
        c = _sqlite_conn()
        try:
            clauses = ["tenant_id=?"]
            params = [tenant_id]
            if result:
                clauses.append("gate_result=?")
                params.append(result)
            if date_from:
                clauses.append("decided_at >= ?")
                params.append(date_from)
            if date_to:
                clauses.append("decided_at <= ?")
                params.append(date_to)
            where = " AND ".join(clauses)
            total = c.execute(f"SELECT COUNT(*) FROM decision_records WHERE {where}", params).fetchone()[0]
            rows = c.execute(
                f"SELECT id, request_id, gate_result, gate_reason, estimated_cost, quota_remaining, decided_at FROM decision_records WHERE {where} ORDER BY decided_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [{"id": r[0], "request_id": r[1], "gate_result": r[2],
                     "gate_reason": r[3], "estimated_cost": r[4],
                     "quota_remaining": r[5], "decided_at": r[6]}
                    for r in rows], total
        finally:
            c.close()

def list_jobs(tenant_id: str, limit: int = 100) -> list[dict]:
    return list_tenant_jobs(tenant_id, limit)


def find_tenant_by_key(api_key: str) -> dict | None:
    """Look up tenant by raw API key. Matches against api_key_hash."""

    if _pg_enabled():
        return _find_tenant_by_key_pg(api_key)
    return _find_tenant_by_key_sqlite(api_key)

def _find_tenant_by_key_pg(api_key_hash: str) -> dict | None:
    conn = _pg_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, plan, api_key FROM tenants WHERE api_key = %s",
            (api_key_hash,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {"tenant_id": row[0], "name": row[1], "tier": row[2], "api_key_hash": row[3]}
    finally:
        _pg_return(conn)

def _find_tenant_by_key_sqlite(api_key_hash: str) -> dict | None:
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT id, name, plan, api_key FROM tenants WHERE api_key = ?",
            (api_key_hash,),
        ).fetchone()
    if not row:
        return None
    return {"tenant_id": row[0], "name": row[1], "plan": row[2], "api_key": row[3]}



# ── Email Verification ──────────────────────────────────────

def create_user_with_password(user_id: str, email: str, name: str, tenant_id: str, api_key: str, password_hash: str, verification_token: str, token_expires) -> dict:
    if _pg_enabled():
        from db_pg_sync import create_user_with_password as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, user_id, email, name, tenant_id, api_key, password_hash, verification_token, token_expires)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    raise RuntimeError("create_user_with_password requires PG")


def get_user_by_verification_token(token: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import get_user_by_verification_token as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, token)
        finally:
            _pg_return(conn)
    import db
    return db.get_user_by_email(token)  # fallback


def verify_user_email(user_id: str) -> None:
    if _pg_enabled():
        from db_pg_sync import verify_user_email as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, user_id)
        finally:
            _pg_return(conn)
    else:
        import db
        # in-memory: just log
        logger.info("email_verified user_id=%s (no PG)", user_id)


def set_verification_token(user_id: str, token: str, expires_at) -> None:
    if _pg_enabled():
        from db_pg_sync import set_verification_token as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, user_id, token, expires_at)
        finally:
            _pg_return(conn)
    else:
        logger.info("set_verification_token user_id=%s (no PG)", user_id)

def create_user_with_email(email: str, password_hash: str, name: str, tenant_id: str, api_key: str) -> dict:
    if _pg_enabled():
        from db_pg_sync import create_email_user as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, email, password_hash, name, tenant_id, api_key)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    raise RuntimeError("email signup requires PostgreSQL")

def mark_user_email_verified(email: str) -> None:
    if _pg_enabled():
        from db_pg_sync import mark_email_verified_pg as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, email)
        finally:
            _pg_return(conn)

def get_user_by_verification_token(token_hash: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import get_user_by_verification_token as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, token_hash)
        finally:
            _pg_return(conn)
    return None

def store_user_verification_token(email: str, token_hash: str, expires_at: str) -> None:
    if _pg_enabled():
        from db_pg_sync import store_verification_token as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, email, token_hash, expires_at)
        finally:
            _pg_return(conn)


# ── Email Verification ────────────────────────────────────────

def create_email_user(user_id: str, email: str, name: str, tenant_id: str, api_key: str, password_hash: str) -> dict:
    if _pg_enabled():
        from db_pg_sync import create_email_user as pg_fn
        conn = _pg_conn()
        try:
            result = pg_fn(conn, user_id, email, name, tenant_id, api_key, password_hash)
            conn.commit()
            return result
        finally:
            _pg_return(conn)
    import db
    return db.upsert_oauth_user(user_id, email, name, "email", tenant_id, api_key)


def set_verification_token(email: str, token: str, expires_at) -> None:
    if _pg_enabled():
        from db_pg_sync import set_verification_token as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, email, token, expires_at)
            conn.commit()
        finally:
            _pg_return(conn)


def find_user_by_verification_token(token: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import find_user_by_verification_token as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, token)
        finally:
            _pg_return(conn)
    return None


def mark_email_verified(email: str) -> None:
    if _pg_enabled():
        from db_pg_sync import mark_email_verified as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, email)
            conn.commit()
        finally:
            _pg_return(conn)


def find_user_by_api_key(api_key: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import find_user_by_api_key as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, api_key)
        finally:
            _pg_return(conn)
    import db
    return db.get_user_by_api_key(api_key)

def update_verification_token(user_id: str, token: str, expires_at: str) -> None:
    if _pg_enabled():
        from db_pg_sync import update_verification_token as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, user_id, token, expires_at)
        finally:
            _pg_return(conn)
        

# ── Invite Codes ─────────────────────────────────────────────────

def create_invite_code(code: str, created_by: str = "admin", max_uses: int = 1, note: str = "", expires_at: str = None) -> dict:
    if _pg_enabled():
        from db_pg_sync import create_invite_code as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, code, created_by, max_uses, note, expires_at)
        finally:
            _pg_return(conn)
    return {}

def validate_invite_code(code: str) -> dict | None:
    if _pg_enabled():
        from db_pg_sync import validate_invite_code as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, code)
        finally:
            _pg_return(conn)
    return None

def use_invite_code(invite_code_id: int, user_id: str) -> None:
    if _pg_enabled():
        from db_pg_sync import use_invite_code as pg_fn
        conn = _pg_conn()
        try:
            pg_fn(conn, invite_code_id, user_id)
        finally:
            _pg_return(conn)

def list_invite_codes() -> list[dict]:
    if _pg_enabled():
        from db_pg_sync import list_invite_codes as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn)
        finally:
            _pg_return(conn)
    return []

def deactivate_invite_code(code: str) -> bool:
    if _pg_enabled():
        from db_pg_sync import deactivate_invite_code as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn, code)
        finally:
            _pg_return(conn)
    return False

def get_beta_config() -> dict:
    if _pg_enabled():
        from db_pg_sync import get_beta_config as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn)
        finally:
            _pg_return(conn)
    return {"max_users": 100, "default_spend_cap_usd": 5.00, "is_active": True}

def count_verified_users() -> int:
    if _pg_enabled():
        from db_pg_sync import count_verified_users as pg_fn
        conn = _pg_conn()
        try:
            return pg_fn(conn)
        finally:
            _pg_return(conn)
    return 0


def record_usage_event(tenant_id: str, event_type: str, value: float,
                       cost_usd: float, job_id: str = "", metadata: dict = None) -> int:
    if _pg_enabled():
        from db_pg_sync import record_usage_event as pg_fn
        conn = _pg_conn()
        try:
            eid = pg_fn(conn, tenant_id, event_type, value, cost_usd, job_id, metadata)
            conn.commit()
            return eid
        finally:
            _pg_return(conn)
    return -1
