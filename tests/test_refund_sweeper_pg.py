"""P1: refund-свипер не работает на PG — литеральный % в LIKE ломает psycopg2.

``get_refund_pending_jobs`` (PG-ветка) выполняет
``... error LIKE 'refund_pending:debit_exists:%' ... LIMIT %s`` с одним
параметром. psycopg2 трактует литеральный ``%`` в LIKE как плейсхолдер →
``IndexError: tuple index out of range`` на каждом цикле (~60 с). SQLite-ветка
(``?``) не затронута.

Тест использует РЕАЛЬНУЮ таблицу ``execution_jobs`` (миграции), вставляет
свою строку с уникальным id и чистит её по ``DELETE`` — НЕ дропает таблицу,
чтобы не ломать остальные PG-тесты, работающие после.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import db_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent


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

    # Idempotent migrations — ensure the real execution_jobs (with error/completed_at).
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "run_migrations.py")],
        check=True,
        capture_output=True,
    )

    jid = f"job-refund-pg-{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO execution_jobs (id, tenant_id, status, error, completed_at) "
                "VALUES (%s, %s, %s, %s, now())",
                (jid, "test-refund-pg", "cancelled", "refund_pending:debit_exists:0.001"),
            )
        rows = db_adapter.get_refund_pending_jobs(limit=50)
        assert jid in [r["id"] for r in rows], rows
    finally:
        with conn.cursor() as c:
            c.execute("DELETE FROM execution_jobs WHERE id = %s", (jid,))
        conn.close()
