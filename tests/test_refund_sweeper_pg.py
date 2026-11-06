"""P1: refund-свипер не работает на PG — литеральный % в LIKE ломает psycopg2.

``get_refund_pending_jobs`` (PG-ветка) выполняет
``... error LIKE 'refund_pending:debit_exists:%' ... LIMIT %s`` с одним
параметром. psycopg2 трактует литеральный ``%`` в LIKE как плейсхолдер →
``IndexError: tuple index out of range`` на каждом цикле (~60 с). SQLite-ветка
(``?``) не затронута.
"""

from __future__ import annotations

import os

import pytest

import db_adapter


def _dsn() -> str | None:
    return os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")


@pytest.mark.pg
def test_get_refund_pending_jobs_pg_like_percent(monkeypatch):
    dsn = _dsn()
    if not dsn:
        pytest.skip(
            "PG not reachable — refund sweeper % bug requires a live PG; issue: C10-refund-sweeper · expiry: 2027-03-31"
        )

    import psycopg2

    monkeypatch.setenv("PG_DSN", dsn)
    monkeypatch.setattr(db_adapter, "_USE_PG", True)
    monkeypatch.setattr(db_adapter, "_PG_POOL", None)

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("DROP TABLE IF EXISTS execution_jobs")
        c.execute(
            "CREATE TABLE execution_jobs ("
            "id TEXT PRIMARY KEY, tenant_id TEXT, status TEXT, error TEXT, "
            "completed_at TIMESTAMPTZ)"
        )
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, completed_at) VALUES (%s, %s, %s, %s, now())",
            ("job-refund-pg", "t1", "cancelled", "refund_pending:debit_exists:0.001"),
        )
    try:
        rows = db_adapter.get_refund_pending_jobs(limit=50)
        assert [r["id"] for r in rows] == ["job-refund-pg"], rows
    finally:
        with conn.cursor() as c:
            c.execute("DROP TABLE IF EXISTS execution_jobs")
        conn.close()
