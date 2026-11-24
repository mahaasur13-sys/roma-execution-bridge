"""Регрессия: PG claim_queued_execution_jobs не захватывает больше `limit` строк.

Дефект master 42a2feed, найден при пересборке 021b: форма
`UPDATE … WHERE id IN (SELECT … LIMIT n FOR UPDATE SKIP LOCKED)` при устаревшей
статистике (ANALYZE на почти пустой выборке, затем много новых queued-строк)
планировалась как Nested Loop Semi Join, подзапрос с LIMIT выполнялся заново на
каждую внешнюю строку, и claim(limit=1) возвращал все queued-строки. Исправление —
`WITH picked AS MATERIALIZED (…) UPDATE … FROM picked`. Тест воспроизводит
устаревшую статистику и требует ровно `limit` строк (самые старые — свои).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db

# 20 queued + 20 completed = 40 изменений после ANALYZE: ниже порога autovacuum analyze
# (50 + 10% строк), поэтому статистика остаётся устаревшей до вызова claim.
_STALE_QUEUED = 20


def _insert(cur, tenant: str, status: str, n: int, offset: int) -> None:
    for i in range(n):
        cur.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, backend, payload, created_at) "
            "VALUES (%s, %s, %s, 'vastai', '{}'::jsonb, "
            "TIMESTAMPTZ '1940-01-01 00:00:00+00' + make_interval(secs => %s))",
            (f"{tenant}-{status}-{offset + i}", tenant, status, offset + i),
        )


@pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG
def test_pg_claim_respects_limit_with_stale_statistics(monkeypatch):
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN не задан; issue: G-CI-PG-CANON · expiry: 2026-12-31")
    import _isolation
    import psycopg2

    assert _isolation.identity(dsn)[2] in _isolation.ALLOWED_DB_NAMES
    monkeypatch.setattr(db, "_USE_PG", True)
    tenant = f"test-claim-limit-{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT current_database()")
        assert cur.fetchone()[0] in _isolation.ALLOWED_DB_NAMES
        _insert(cur, tenant, "queued", 1, 0)
        _insert(cur, tenant, "completed", 1, 0)
        cur.execute("ANALYZE execution_jobs")  # статистика снята до массовой вставки
        _insert(cur, tenant, "queued", _STALE_QUEUED, 1)
        _insert(cur, tenant, "completed", _STALE_QUEUED, 1)

        claimed_total = 0
        for limit in (1, 3):
            rows = db.claim_queued_execution_jobs(limit=limit)
            assert len(rows) == limit, f"claim(limit={limit}) захватил {len(rows)} строк"
            assert all(r["tenant_id"] == tenant and r["status"] == "running" for r in rows)
            claimed_total += len(rows)
        cur.execute("SELECT count(*) FROM execution_jobs WHERE tenant_id=%s AND status='running'", (tenant,))
        assert cur.fetchone()[0] == claimed_total
    finally:
        cur.execute("DELETE FROM execution_jobs WHERE tenant_id=%s", (tenant,))
        conn.close()
