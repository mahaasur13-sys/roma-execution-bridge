"""021b: аренда задачи воркером — bind на claim, вердикт в /complete и worker-ack.

Только local. ENFORCE=0 по умолчанию (shadow): несовпадение пишет аудит
`job.lease.shadow_mismatch` и не блокирует. ENFORCE=1 (только тесты): отказ
до finalize, дебета нет. Токен живёт в ответе выдачи задачи; в БД — sha256.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import job_lease
import main
import router_jobs
from billing import finalize as fin
from deps import register_key

WORKER_TOKEN = "lease-021b-worker-token"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class FakeLedger:
    def __init__(self, balance: float = 100.0):
        self.balance = balance
        self.calls = 0

    def get_tenant_balance(self, tenant_id):
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        self.calls += 1
        if self.balance < amount:
            return None, False
        self.balance -= amount
        return "led-" + uuid.uuid4().hex[:8], True


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def world(monkeypatch):
    tenant_id = _uniq("test-lease")
    key = _uniq("key-lease")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "L21b"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    original = dict(main.API_KEYS)
    register_key(key, {"tenant_id": tenant_id})

    jobs: dict[str, dict] = {}
    audits: list[tuple] = []

    def _get(job_id):
        j = jobs.get(job_id)
        return dict(j) if j else None

    def _update(job_id, status=None, completed_at=None, error=None, tenant_id=None, if_status_not_in=None, **kw):
        j = jobs.get(job_id)
        if not j:
            return 0
        if if_status_not_in and j.get("status") in if_status_not_in:
            return 0
        if status is not None:
            j["status"] = status
        if completed_at is not None:
            j["completed_at"] = completed_at
        if error is not None:
            j["error"] = error
        return 1

    monkeypatch.setattr(db, "get_execution_job", _get)
    monkeypatch.setattr(db, "update_execution_job", _update)

    async def _confirmed(job_id, backend_name=None):
        return 60.0

    monkeypatch.setattr(router_jobs, "get_confirmed_duration", _confirmed)
    monkeypatch.setattr(
        router_jobs,
        "write_audit_event",
        lambda tenant, event_type, entity_type, entity_id, data=None: audits.append((event_type, data or {})),
    )
    monkeypatch.setenv("ROMA_GPU_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.delenv("ROMA_LEASE_ENFORCE", raising=False)

    ledger = FakeLedger()
    monkeypatch.setattr(fin, "billing_ledger", ledger)
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))

    def add_job(
        *, bound=False, token="secret-token-0123456789abcdef", workers=None, epoch=1, expires_in=600, status="running"
    ):
        jid = _uniq("job-lease")
        job = {
            "job_id": jid,
            "tenant_id": tenant_id,
            "status": status,
            "backend": "local",
            "payload": {},
            "started_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
        }
        if bound:
            job["worker_id"] = workers or job_lease.local_worker_id()
            job["lease_token_hash"] = job_lease.hash_lease_token(token) if token else None
            job["lease_epoch"] = epoch
            if expires_in is None:
                job["lease_expires_at"] = None
            else:
                job["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
        else:
            job["worker_id"] = None
            job["lease_token_hash"] = None
            job["lease_epoch"] = 0
            job["lease_expires_at"] = None
        jobs[jid] = job
        return jid

    yield {
        "tenant_id": tenant_id,
        "key": key,
        "jobs": jobs,
        "audits": audits,
        "ledger": ledger,
        "add_job": add_job,
        "client": TestClient(main.app, raise_server_exceptions=False),
    }

    main.API_KEYS.clear()
    main.API_KEYS.update(original)


@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lease.db")
    return db


def _audit_types(world):
    return [t for t, _ in world["audits"]]


# ── bind на claim ───────────────────────────────────────────────────────────


def test_claim_binds_local_job_and_returns_token(sqlite_db):
    c = sqlite_db._sqlite_conn()
    try:
        sqlite_db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, payload, created_at) VALUES (?,?,?,?,?)",
            ("j-local", "t", "queued", '{"backend": "local"}', "2026-01-01 00:00:00"),
        )
        c.commit()
    finally:
        c.close()

    (claimed,) = sqlite_db.claim_queued_execution_jobs(limit=5)
    assert claimed["status"] == "running"
    assert claimed["worker_id"] == job_lease.local_worker_id()
    assert claimed["lease_epoch"] == 1
    assert claimed["lease_expires_at"]
    assert claimed["lease_token"]
    assert "lease_token_hash" not in claimed
    row = sqlite_db.get_execution_job("j-local")
    assert row["lease_token_hash"] == job_lease.hash_lease_token(claimed["lease_token"])
    assert row["lease_token_hash"] != claimed["lease_token"]


def test_claim_leaves_other_backends_unbound(sqlite_db):
    c = sqlite_db._sqlite_conn()
    try:
        sqlite_db._ensure_execution_jobs_table(c)
        for jid, payload in (("j-vast", '{"backend": "vastai"}'), ("j-none", '{"k": 1}')):
            c.execute(
                "INSERT INTO execution_jobs (id, tenant_id, status, payload, created_at) VALUES (?,?,?,?,?)",
                (jid, "t", "queued", payload, "2026-01-01 00:00:00"),
            )
        c.commit()
    finally:
        c.close()

    claimed = sqlite_db.claim_queued_execution_jobs(limit=5)
    assert {j["id"] for j in claimed} == {"j-vast", "j-none"}
    for job in claimed:
        assert job["worker_id"] is None
        assert job["lease_epoch"] == 0
        assert "lease_token" not in job


def test_second_claim_does_not_rebind(sqlite_db):
    c = sqlite_db._sqlite_conn()
    try:
        sqlite_db._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, payload, created_at) VALUES (?,?,?,?,?)",
            ("j-rebind", "t", "queued", '{"backend": "local"}', "2026-01-01 00:00:00"),
        )
        c.commit()
    finally:
        c.close()

    (first,) = sqlite_db.claim_queued_execution_jobs(limit=5)
    assert sqlite_db.claim_queued_execution_jobs(limit=5) == []
    assert not sqlite_db.bind_worker_lease("j-rebind", "other-worker", "deadbeef", 60)
    row = sqlite_db.get_execution_job("j-rebind")
    assert row["worker_id"] == first["worker_id"]
    assert row["lease_epoch"] == 1
    assert row["lease_token_hash"] == job_lease.hash_lease_token(first["lease_token"])


# ── shadow (ENFORCE=0) ──────────────────────────────────────────────────────


def test_shadow_mismatch_audits_and_completes(world):
    jid = world["add_job"](bound=True)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/complete",
        json={"lease_token": "wrong-token"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert world["ledger"].calls == 1
    assert "job.lease.shadow_mismatch" in _audit_types(world)
    reason = dict(world["audits"])["job.lease.shadow_mismatch"]["reason"]
    assert reason == "bad_token"


def test_shadow_expired_lease_still_completes(world):
    jid = world["add_job"](bound=True, expires_in=-30)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/complete",
        json={"lease_token": "secret-token-0123456789abcdef"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert dict(world["audits"])["job.lease.shadow_mismatch"]["reason"] == "expired"


def test_shadow_missing_token_audits_no_token(world):
    jid = world["add_job"](bound=True)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert world["ledger"].calls == 1
    assert dict(world["audits"])["job.lease.shadow_mismatch"]["reason"] == "no_token"


# ── enforce (ENFORCE=1, только тесты) ───────────────────────────────────────


def test_enforce_missing_token_denied_before_finalize(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/complete",
        json={},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 409, resp.text
    assert "lease_denied" in resp.text
    assert world["ledger"].calls == 0
    assert world["jobs"][jid]["status"] == "running"
    assert "job.lease.denied" in _audit_types(world)


def test_enforce_foreign_worker_id_denied(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": "somebody-else", "lease_token": "secret-token-0123456789abcdef"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert dict(world["audits"])["job.lease.denied"]["reason"] == "worker_mismatch"
    assert world["ledger"].calls == 0


def test_enforce_wrong_token_denied(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "lease_token": "not-the-token"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert world["ledger"].calls == 0
    assert world["jobs"][jid]["status"] == "running"


def test_enforce_expired_lease_denied(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True, expires_in=-1)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/complete",
        json={"lease_token": "secret-token-0123456789abcdef"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 409, resp.text
    assert world["ledger"].calls == 0


def test_enforce_stale_epoch_denied(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True, epoch=2)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/complete",
        json={"lease_token": "secret-token-0123456789abcdef", "lease_epoch": 1},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 409, resp.text
    assert dict(world["audits"])["job.lease.denied"]["reason"] == "stale_epoch"
    assert world["ledger"].calls == 0


def test_enforce_correct_token_finalizes_once(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True)
    payload = {"lease_token": "secret-token-0123456789abcdef", "lease_epoch": 1}
    first = world["client"].post(f"/v1/jobs/{jid}/complete", json=payload, headers={"X-API-Key": world["key"]})
    assert first.status_code == 200, first.text
    assert world["ledger"].calls == 1
    second = world["client"].post(f"/v1/jobs/{jid}/complete", json=payload, headers={"X-API-Key": world["key"]})
    assert second.status_code == 409, second.text
    assert world["ledger"].calls == 1
    assert world["jobs"][jid]["status"] == "completed"


def test_enforce_unbound_job_unchanged(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=False)
    resp = world["client"].post(f"/v1/jobs/{jid}/complete", json={}, headers={"X-API-Key": world["key"]})
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert world["audits"] == []


def test_token_never_reaches_audit_or_error_column(world):
    jid = world["add_job"](bound=True)
    token = "secret-token-0123456789abcdef"
    world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "fail", "error": "boom", "lease_token": token},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert world["jobs"][jid].get("error") == "boom"
    for _event, data in world["audits"]:
        assert token not in str(data)
    assert token not in str(world["jobs"][jid])
