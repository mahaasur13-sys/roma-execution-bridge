"""A2: db_adapter is the single DB facade (backend selected by env)."""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_facade_selects_sqlite_without_pg(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", None)  # reset cached backend selection
    assert db._pg_enabled() is False


def test_facade_create_get_list_job(monkeypatch):
    """One facade exposes create/get/list for execution jobs on SQLite."""
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", None)

    jid = _uniq("job")
    tid = _uniq("t")

    db.insert_execution_job(
        job_id=jid, decision_id=_uniq("dec"), tenant_id=tid, status="queued", payload={}
    )
    job = db.get_execution_job(jid)
    assert job["id"] == jid
    assert job["tenant_id"] == tid
    assert job["status"] == "queued"

    jobs = db.list_tenant_jobs(tid, limit=10)
    assert any(j["id"] == jid for j in jobs)
