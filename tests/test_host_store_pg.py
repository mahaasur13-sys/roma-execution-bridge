"""PgHostStore against a real PostgreSQL (migrations 019 + 020 applied). env-skip without PG_DSN."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gpu_worker import host_bench
from gpu_worker import host_registry as hr
from gpu_worker.host_api import HostApi

TH = {"RTX_3060": {"min_tflops": 1e-9, "max_tflops": 1e9}}  # test-only bounds
MIG = Path(__file__).resolve().parent.parent / "migrations" / "020_host_agent_tokens.sql"


def _conn():
    import psycopg2

    return psycopg2.connect(os.environ["PG_DSN"])


def _cleanup(host_ids):
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            for t in ("host_job_tokens", "host_tokens", "host_status_events", "host_verifications"):
                cur.execute(f"DELETE FROM {t} WHERE host_id = ANY(%s)", (list(host_ids),))  # nosec B608 - fixed names
            cur.execute("DELETE FROM hosts WHERE host_id = ANY(%s)", (list(host_ids),))
    finally:
        conn.close()


@pytest.mark.pg
def test_migration_020_is_idempotent():
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(MIG.read_text())
        cur.execute(MIG.read_text())
        cur.execute("SELECT to_regclass('host_tokens'), to_regclass('host_job_tokens')")
        assert all(cur.fetchone())
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.pg
def test_pg_store_roundtrip_reload_and_single_use(monkeypatch):
    from gpu_worker.host_store import PgHostStore

    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    store = PgHostStore()
    assert "dsn=***" in repr(store)
    reg = hr.HostRegistry(store=store)
    api = HostApi(reg, store=store)
    h = reg.apply(owner_contact_ref="r", gpu_model="RTX_3060", gpu_count=1, vram_gb=12, region="EU", own_host=True)
    try:
        tok = api.issue_host_token(h.host_id)
        reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)

        # "restart": a fresh registry + api rebuilt from PG only
        reg2 = hr.HostRegistry(store=store)
        assert reg2.load_from_store() >= 1
        api2 = HostApi(reg2, store=store)
        h2 = reg2.get(h.host_id)
        assert h2.status == hr.VERIFYING and h2.pending_verification is not None
        job = api2.handle({"action": "fetch_job", "host_id": h.host_id, "token": tok})["job"]
        sub = {
            "action": "submit_result",
            "host_id": h.host_id,
            "token": tok,
            "job_id": job["job_id"],
            "job_token": job["job_token"],
            "output_sha256": host_bench.reference_hash(job["seed"], job["n"]),
            "elapsed_s": 0.5,
        }
        assert api2.handle(dict(sub))["code"] == 200
        assert api2.handle(dict(sub))["code"] == 409  # consumed in PG
        assert store.consume_job_token(job["job_id"]) is None

        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT status, trust_tier FROM hosts WHERE host_id=%s", (h.host_id,))
            assert cur.fetchone() == ("active", "verified")
            cur.execute("SELECT passed, n, reps FROM host_verifications WHERE host_id=%s", (h.host_id,))
            assert cur.fetchone() == (True, 8, 1)
            cur.execute("SELECT token_sha256 FROM host_tokens WHERE host_id=%s", (h.host_id,))
            stored = cur.fetchone()[0]
            assert stored != tok and len(stored) == 64
            cur.execute("SELECT count(*) FROM host_status_events WHERE host_id=%s", (h.host_id,))
            assert cur.fetchone()[0] >= 2
        finally:
            conn.close()

        api2.revoke_host_token(h.host_id)
        api3 = HostApi(hr.HostRegistry(store=store), store=store)
        assert api3.handle({"action": "heartbeat", "host_id": h.host_id, "token": tok})["code"] == 401
    finally:
        _cleanup([h.host_id])
