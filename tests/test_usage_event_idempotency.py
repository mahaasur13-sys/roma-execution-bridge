"""RaaS-01 / migration 016: idempotent usage events for real job_id.

``(tenant_id, job_id, event_type)`` is unique for REAL job_ids; ``manual`` and
``auto`` are not unique identifiers and stay plain (non-deduplicated) inserts.
Repeated writes are a no-op and do NOT update ``cost_usd``/``billed``.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pg_dsn() -> str | None:
    return os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL")


@pytest.mark.pg
def test_usage_event_idempotency():
    dsn = _pg_dsn()
    if not dsn:
        pytest.skip(
            "PG not reachable — usage-event idempotency requires a live PG; "
            "issue: RaaS-01 · expiry: 2026-12-31"
        )
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "run_migrations.py")],
        check=True,
        capture_output=True,
    )

    import psycopg2

    from billing.pg_metering import PGMeteringEngine

    engine = PGMeteringEngine()
    tenant = f"raas-idem-{uuid.uuid4().hex[:8]}"
    job = f"job-{uuid.uuid4().hex[:8]}"

    # 1. same (tenant, job, event_type) twice -> ONE row; repeat does not update.
    engine.record("gpu_usage", tenant=tenant, gpu_seconds=10.0, job_id=job,
                  cost_usd=0.0001, billed=True)
    engine.record("gpu_usage", tenant=tenant, gpu_seconds=99.0, job_id=job,
                  cost_usd=9.99, billed=False)

    # 2. gpu_usage + token_usage on the same job -> TWO rows.
    engine.record("token_usage", tenant=tenant, job_id=job, value=100.0,
                  cost_usd=0.1, billed=True)

    # 3. manual job_id twice -> TWO rows (not collapsed).
    engine.record("gpu_usage", tenant=tenant, gpu_seconds=5.0, job_id="manual",
                  billed=False)
    engine.record("gpu_usage", tenant=tenant, gpu_seconds=5.0, job_id="manual",
                  billed=False)

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM usage_events "
        "WHERE tenant_id=%s AND job_id=%s AND event_type='gpu_usage'",
        (tenant, job),
    )
    assert cur.fetchone()[0] == 1

    cur.execute(
        "SELECT cost_usd, billed FROM usage_events "
        "WHERE tenant_id=%s AND job_id=%s AND event_type='gpu_usage'",
        (tenant, job),
    )
    cost_usd, billed = cur.fetchone()
    assert cost_usd == 0.0001  # repeat did NOT overwrite
    assert billed is True       # repeat did NOT overwrite

    cur.execute(
        "SELECT COUNT(*) FROM usage_events WHERE tenant_id=%s AND job_id=%s",
        (tenant, job),
    )
    assert cur.fetchone()[0] == 2  # gpu + token

    cur.execute(
        "SELECT COUNT(*) FROM usage_events "
        "WHERE tenant_id=%s AND job_id='manual' AND event_type='gpu_usage'",
        (tenant,),
    )
    assert cur.fetchone()[0] == 2  # manual NOT collapsed
    conn.close()
