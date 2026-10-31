"""Isolated tests for routers/submit.py (no debit at submit — debit is in finalize).

Fully offline: the DB adapter, API-key lookup and the decision gate are replaced
with in-memory doubles via monkeypatch. No network, no PostgreSQL, no live backends.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import db_adapter
import main
import routers.submit as submit_mod
from models.decision import GateResult

# Obviously fake, low-entropy test credentials assembled at runtime (not secrets).
KEY_A = "-".join(["fake", "tenant", "alpha", "only"])
KEY_B = "-".join(["fake", "tenant", "bravo", "only"])
TENANTS = {KEY_A: "tenant-a", KEY_B: "tenant-b"}


class FakeDB:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.idem: dict[tuple[str, str], str] = {}
        self.inserts = 0

    def find_tenant_by_key(self, api_key):
        tid = TENANTS.get(api_key)
        return {"tenant_id": tid, "plan": "pro"} if tid else None

    def find_job_by_idempotency(self, tenant_id, key):
        return self.idem.get((tenant_id, key))

    def create_job_idempotency(self, tenant_id, key, job_id):
        if (tenant_id, key) in self.idem:
            return False
        self.idem[(tenant_id, key)] = job_id
        return True

    def get_execution_job(self, job_id):
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    def insert_execution_job(self, job_id, decision_id, tenant_id, status, payload):
        self.inserts += 1
        self.jobs[job_id] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "status": status,
            "payload": payload,
            "created_at": "2026-10-31T00:00:00+00:00",
        }
        return self.jobs[job_id]

    def update_execution_job(self, job_id, tenant_id=None, **fields):
        job = self.jobs.get(job_id)
        if not job or (tenant_id is not None and job["tenant_id"] != tenant_id):
            return 0
        fields.pop("if_status_not_in", None)
        job.update(fields)
        return 1

    def count_jobs_active_for_tenant(self, tenant_id):
        return sum(1 for j in self.jobs.values() if j["tenant_id"] == tenant_id)


class FakeGate:
    def __init__(self, result=GateResult.ALLOWED, reason="ok", exc=None):
        self.result, self.reason, self.exc = result, reason, exc
        self.calls = []

    def evaluate(self, tenant_id, payload):
        self.calls.append(tenant_id)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(result=self.result, reason=self.reason)


@pytest.fixture
def fake_db(monkeypatch):
    fdb = FakeDB()
    for name in (
        "find_tenant_by_key",
        "find_job_by_idempotency",
        "create_job_idempotency",
        "get_execution_job",
        "insert_execution_job",
        "update_execution_job",
        "count_jobs_active_for_tenant",
    ):
        monkeypatch.setattr(db_adapter, name, getattr(fdb, name))
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: None)
    monkeypatch.setattr(db_adapter, "log_user_event", lambda *a, **k: None)
    monkeypatch.setattr(main, "is_email_verified", lambda key: True)
    monkeypatch.setattr(submit_mod, "on_job_created", lambda *a, **k: None)
    monkeypatch.delenv("JEV_ENABLED", raising=False)
    main.limiter.reset()
    return fdb


def _gate(monkeypatch, gate):
    monkeypatch.setattr(main, "_get_gate", lambda: gate)
    return gate


@pytest.fixture
def client(fake_db):
    return TestClient(main.app, raise_server_exceptions=False)


BODY = {"task": "train", "backend": "local"}


def test_submit_202_ok(client, fake_db, monkeypatch):
    gate = _gate(monkeypatch, FakeGate())
    r = client.post("/submit", json=BODY, headers={"X-API-Key": KEY_A})
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "queued"
    job_id = data["job_id"]
    assert gate.calls == ["tenant-a"]
    assert fake_db.inserts == 1
    job = fake_db.jobs[job_id]
    assert job["tenant_id"] == "tenant-a"
    assert job["status"] == "queued"
    assert job["backend"] == "local"


def test_submit_idempotency_same_key_no_duplicate_job(client, fake_db, monkeypatch):
    _gate(monkeypatch, FakeGate())
    h = {"X-API-Key": KEY_A, "Idempotency-Key": "idem-123"}
    r1 = client.post("/submit", json=BODY, headers=h)
    r2 = client.post("/submit", json=BODY, headers=h)
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert fake_db.inserts == 1
    assert len(fake_db.jobs) == 1
    assert fake_db.idem == {("tenant-a", "idem-123"): r1.json()["job_id"]}


def test_submit_gate_deny_fail_closed(client, fake_db, monkeypatch):
    _gate(monkeypatch, FakeGate(result=GateResult.DENIED, reason="quota exceeded"))
    r = client.post("/submit", json=BODY, headers={"X-API-Key": KEY_A, "Idempotency-Key": "idem-deny"})
    assert r.status_code == 402, r.text
    assert fake_db.inserts == 0 and fake_db.jobs == {}
    assert fake_db.idem == {}, "denied submit must not reserve the idempotency key"

    # Fail-closed: a gate that errors must never yield an accepted job.
    _gate(monkeypatch, FakeGate(exc=RuntimeError("gate backend down")))
    r = client.post("/submit", json=BODY, headers={"X-API-Key": KEY_A})
    assert r.status_code >= 500, r.text
    assert fake_db.inserts == 0 and fake_db.jobs == {}


def test_submit_tenant_scoped(client, fake_db, monkeypatch):
    _gate(monkeypatch, FakeGate())
    r = client.post("/submit", json=BODY, headers={"X-API-Key": KEY_A, "Idempotency-Key": "shared"})
    assert r.status_code == 202, r.text
    job_a = r.json()["job_id"]

    assert client.get(f"/status/{job_a}", headers={"X-API-Key": KEY_A}).status_code == 200
    r = client.get(f"/status/{job_a}", headers={"X-API-Key": KEY_B})
    assert r.status_code == 404, r.text

    # Same Idempotency-Key from another tenant must not replay tenant A's job.
    r = client.post("/submit", json=BODY, headers={"X-API-Key": KEY_B, "Idempotency-Key": "shared"})
    assert r.status_code == 202, r.text
    assert r.json()["job_id"] != job_a
    assert fake_db.jobs[job_a]["tenant_id"] == "tenant-a"


def test_submit_mask_key(client, fake_db, monkeypatch):
    assert main._mask_key(KEY_A) == KEY_A[:4] + "***" + KEY_A[-4:]
    assert main._mask_key("short") == "***"
    assert main._mask_key(None) is None

    _gate(monkeypatch, FakeGate())
    # Capture by replacing the logger methods (not via handler/caplog): another test
    # calls logging.disable(CRITICAL) for the whole session and "roma" has propagate=False.
    calls: list[tuple[str, tuple, dict]] = []
    for lg in {main.logger, submit_mod.logger}:
        for level in ("debug", "info", "warning", "error", "exception"):
            monkeypatch.setattr(
                lg,
                level,
                lambda msg, *args, _l=level, **kw: calls.append((str(msg) % args if args else str(msg), args, kw)),
            )
    r = client.post("/submit", json=BODY, headers={"X-API-Key": KEY_A})
    assert r.status_code == 202, r.text

    masked = KEY_A[:4] + "***" + KEY_A[-4:]
    req_logs = [kw["extra"] for _, _, kw in calls if (kw.get("extra") or {}).get("endpoint") == "/submit"]
    assert req_logs, "request log for /submit not emitted"
    assert all(extra["api_key"] == masked for extra in req_logs)
    for call in calls:
        assert KEY_A not in repr(call)
