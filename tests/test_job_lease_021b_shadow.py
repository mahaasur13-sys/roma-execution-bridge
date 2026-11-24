"""021b: аренда задачи воркером — bind на claim, вердикт в /complete и worker-ack.

Только local. ENFORCE=0 по умолчанию (shadow): несовпадение пишет аудит
`job.lease.shadow_mismatch` и не блокирует. ENFORCE=1 (только тесты): отказ
до finalize, дебета нет. Исходный токен local-аренды держит только сервер
(реестр job_lease, D-02); в БД — sha256, клиенту и в ответ claim он не уходит.
Решения владельца D-02…D-06 (24.11.2026) покрыты тестами в конце файла.
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
from routers import public_jobs
from billing import finalize as fin
from deps import register_key

WORKER_TOKEN = "lease-021b-worker-token"
TOKEN = "secret-token-0123456789abcdef"
GPU_WORKER = "gpu-worker-1"


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
    monkeypatch.setattr(public_jobs, "get_confirmed_duration", _confirmed)
    monkeypatch.setenv("ROMA_GPU_WORKER_TOKEN", WORKER_TOKEN)
    monkeypatch.delenv("ROMA_LEASE_ENFORCE", raising=False)
    monkeypatch.setattr(job_lease, "_SERVER_LEASES", {})

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
    monkeypatch.setattr(job_lease, "_SERVER_LEASES", {})
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "lease.db")
    return db


def _audit_types(world):
    return [t for t, _ in world["audits"]]


# ── bind на claim ───────────────────────────────────────────────────────────


def test_claim_binds_local_job_token_stays_on_server(sqlite_db):
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
    assert "lease_token" not in claimed  # D-02: токен не покидает сервер
    assert "lease_token_hash" not in claimed
    lease = job_lease.server_lease("j-local")
    assert lease and lease["epoch"] == 1 and lease["worker_id"] == job_lease.local_worker_id()
    row = sqlite_db.get_execution_job("j-local")
    assert row["lease_token_hash"] == job_lease.hash_lease_token(lease["token"])
    assert row["lease_token_hash"] != lease["token"]


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
        assert job_lease.server_lease(job["id"]) is None


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
    assert row["lease_token_hash"] == job_lease.hash_lease_token(job_lease.server_lease("j-rebind")["token"])


# ── shadow (ENFORCE=0) ──────────────────────────────────────────────────────


def test_shadow_mismatch_audits_and_completes(world):
    jid = world["add_job"](bound=True, workers=GPU_WORKER)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": GPU_WORKER, "lease_token": "wrong-token"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert world["ledger"].calls == 1
    assert "job.lease.shadow_mismatch" in _audit_types(world)
    reason = dict(world["audits"])["job.lease.shadow_mismatch"]["reason"]
    assert reason == "bad_token"


def test_shadow_expired_lease_still_completes(world):
    jid = world["add_job"](bound=True, workers=GPU_WORKER, expires_in=-30)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": GPU_WORKER, "lease_token": TOKEN},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
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
    """D-05: истечение аренды само по себе не даёт списания."""
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True, workers=GPU_WORKER, expires_in=-1)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": GPU_WORKER, "lease_token": TOKEN},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert dict(world["audits"])["job.lease.denied"]["reason"] == "expired"
    assert world["ledger"].calls == 0
    assert world["jobs"][jid]["status"] == "running"


def test_enforce_stale_epoch_denied(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True, workers=GPU_WORKER, epoch=2)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": GPU_WORKER, "lease_token": TOKEN, "lease_epoch": 1},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert dict(world["audits"])["job.lease.denied"]["reason"] == "stale_epoch"
    assert world["ledger"].calls == 0


def test_enforce_correct_token_finalizes_once(world, monkeypatch):
    """D-02: bound local закрывает сервер по своему токену; клиент шлёт пустой запрос."""
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True)
    job_lease.remember_server_lease(jid, TOKEN, 1, job_lease.local_worker_id())
    payload = {}
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


# ── D-02: local-задачу закрывает доверенный серверный путь ──────────────────


def test_d02_client_supplied_token_is_ignored_for_local_binding(world, monkeypatch):
    """Клиент не держит токен local-аренды: даже верный токен из запроса не используется."""
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/complete",
        json={"lease_token": TOKEN, "lease_epoch": 1, "worker_id": job_lease.local_worker_id()},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 409, resp.text
    denied = dict(world["audits"])["job.lease.denied"]
    assert denied["reason"] == "no_token" and denied["path"] == "complete_local_server"
    assert world["ledger"].calls == 0
    assert world["jobs"][jid]["status"] == "running"


def test_d02_server_lease_forgotten_after_completion(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True)
    job_lease.remember_server_lease(jid, TOKEN, 1, job_lease.local_worker_id())
    resp = world["client"].post(f"/v1/jobs/{jid}/complete", json={}, headers={"X-API-Key": world["key"]})
    assert resp.status_code == 200, resp.text
    assert job_lease.server_lease(jid) is None
    assert world["ledger"].calls == 1


def test_d02_server_path_stale_epoch_denied(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True, epoch=2)
    job_lease.remember_server_lease(jid, TOKEN, 1, job_lease.local_worker_id())
    resp = world["client"].post(f"/v1/jobs/{jid}/complete", json={}, headers={"X-API-Key": world["key"]})
    assert resp.status_code == 409, resp.text
    assert dict(world["audits"])["job.lease.denied"]["reason"] == "stale_epoch"
    assert world["ledger"].calls == 0


# ── D-03: legacy POST /complete/{id} → 409 для bound, fail-closed ───────────


def test_d03_legacy_complete_bound_job_409_before_billing(world):
    jid = world["add_job"](bound=True)
    job_lease.remember_server_lease(jid, TOKEN, 1, job_lease.local_worker_id())
    before = dict(world["jobs"][jid])
    resp = world["client"].post(f"/complete/{jid}", headers={"X-API-Key": world["key"]})
    assert resp.status_code == 409, resp.text
    assert "lease_bound_legacy_complete_denied" in resp.text
    assert world["ledger"].calls == 0
    assert world["jobs"][jid] == before


def test_d03_legacy_complete_binding_unknown_fail_closed(world):
    jid = world["add_job"](bound=False)
    del world["jobs"][jid]["worker_id"]
    resp = world["client"].post(f"/complete/{jid}", headers={"X-API-Key": world["key"]})
    assert resp.status_code == 409, resp.text
    assert "lease_binding_unknown" in resp.text
    assert world["ledger"].calls == 0
    assert world["jobs"][jid]["status"] == "running"


def test_d03_legacy_complete_inconsistent_binding_fail_closed(world):
    jid = world["add_job"](bound=False)
    world["jobs"][jid]["lease_token_hash"] = job_lease.hash_lease_token(TOKEN)
    resp = world["client"].post(f"/complete/{jid}", headers={"X-API-Key": world["key"]})
    assert resp.status_code == 409, resp.text
    assert "lease_binding_unknown" in resp.text
    assert world["ledger"].calls == 0


def test_d03_legacy_complete_unbound_unchanged(world, monkeypatch):
    async def _no_backend(**kw):
        return None

    monkeypatch.setattr(public_jobs, "backend_cancel_job", _no_backend)
    jid = world["add_job"](bound=False)
    resp = world["client"].post(f"/complete/{jid}", headers={"X-API-Key": world["key"]})
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert world["ledger"].calls == 1


@pytest.mark.parametrize(
    ("job", "state"),
    [
        ({"worker_id": None, "lease_epoch": 0}, "unbound"),
        ({"worker_id": "", "lease_epoch": 0, "lease_token_hash": None}, "unbound"),
        ({"worker_id": "w-1"}, "bound"),
        ({}, "unknown"),
        ({"worker_id": 42}, "unknown"),
        ({"worker_id": None, "lease_epoch": 3}, "unknown"),
        ({"worker_id": None, "lease_epoch": "x"}, "unknown"),
    ],
)
def test_d03_binding_state(job, state):
    assert job_lease.binding_state(job) == state


# ── D-04: shadow pass-through при сбое аудита; enforce — отказ ──────────────


def _lease_audit_down(world):
    """Падает только запись событий аренды; прочие аудит-события — в список."""

    def _write(tenant, event_type, entity_type, entity_id, data=None):
        if event_type.startswith("job.lease."):
            raise RuntimeError("audit db down")
        world["audits"].append((event_type, data or {}))

    return _write


def _metric(name, **labels):
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_d04_shadow_audit_failure_passes_through(world, monkeypatch):
    monkeypatch.setattr(router_jobs, "write_audit_event", _lease_audit_down(world))
    before = _metric("roma_job_lease_audit_write_failed_total", mode="shadow")
    jid = world["add_job"](bound=True, workers=GPU_WORKER)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": GPU_WORKER, "lease_token": "wrong"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "completed"
    assert world["ledger"].calls == 1
    assert _metric("roma_job_lease_audit_write_failed_total", mode="shadow") == before + 1


def test_d04_enforce_audit_failure_still_denies_before_finalize(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    monkeypatch.setattr(router_jobs, "write_audit_event", _lease_audit_down(world))
    before = _metric("roma_job_lease_audit_write_failed_total", mode="enforce")
    jid = world["add_job"](bound=True, workers=GPU_WORKER)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": GPU_WORKER, "lease_token": "wrong"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert world["ledger"].calls == 0
    assert world["jobs"][jid]["status"] == "running"
    assert _metric("roma_job_lease_audit_write_failed_total", mode="enforce") == before + 1


def test_d04_verdict_metric_counted_server_side(world):
    labels = {"path": "worker_ack", "mode": "shadow", "reason": "bad_token"}
    before = _metric("roma_job_lease_verdict_total", **labels)
    jid = world["add_job"](bound=True, workers=GPU_WORKER)
    world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": GPU_WORKER, "lease_token": "wrong"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert _metric("roma_job_lease_verdict_total", **labels) == before + 1


def test_d04_repeated_shadow_mismatch_not_deduplicated(sqlite_db):
    """Реальный INSERT audit_events: повторы по одной задаче сохраняются."""
    job = {"worker_id": GPU_WORKER, "lease_epoch": 1, "status": "running"}
    data = job_lease.audit_data(job, "bad_token")
    for _ in range(3):
        assert router_jobs._write_lease_audit("test-t", "job.lease.shadow_mismatch", "j-dup", data, "shadow")
    c = sqlite_db._sqlite_conn()
    try:
        rows = c.execute(
            "SELECT entity_id, data FROM audit_events WHERE event_type='job.lease.shadow_mismatch'"
        ).fetchall()
    finally:
        c.close()
    assert len(rows) == 3
    assert len({r[0] for r in rows}) == 3
    assert all('"job_id": "j-dup"' in r[1] for r in rows)


def test_d04_repeat_completion_no_second_debit(world):
    jid = world["add_job"](bound=True, workers=GPU_WORKER)
    body = {"action": "complete", "worker_id": GPU_WORKER, "lease_token": "wrong"}
    hdr = {"X-Roma-Worker-Token": WORKER_TOKEN}
    assert world["client"].post(f"/v1/jobs/{jid}/worker-ack", json=body, headers=hdr).status_code == 200
    assert world["client"].post(f"/v1/jobs/{jid}/worker-ack", json=body, headers=hdr).status_code == 409
    assert world["ledger"].calls == 1


# ── D-05: claim+bind одной транзакцией; TTL; продление ─────────────────────


def _insert_local(db_mod, jid, status="queued"):
    c = db_mod._sqlite_conn()
    try:
        db_mod._ensure_execution_jobs_table(c)
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, payload, created_at) VALUES (?,?,?,?,?)",
            (jid, "t", status, '{"backend": "local"}', "2026-01-01 00:00:00"),
        )
        c.commit()
    finally:
        c.close()


def test_d05_bind_failure_rolls_back_claim(sqlite_db, monkeypatch):
    _insert_local(sqlite_db, "j-atomic")

    def _fail():
        raise RuntimeError("bind boom")

    original = job_lease.local_worker_id
    monkeypatch.setattr(job_lease, "local_worker_id", _fail)
    with pytest.raises(RuntimeError):
        sqlite_db.claim_queued_execution_jobs(limit=5)
    row = sqlite_db.get_execution_job("j-atomic")
    assert row["status"] == "queued"
    assert row["started_at"] is None
    assert row["worker_id"] is None and row["lease_epoch"] == 0 and row["lease_token_hash"] is None
    assert job_lease.server_lease("j-atomic") is None

    monkeypatch.setattr(job_lease, "local_worker_id", original)  # повтор после ошибки
    (claimed,) = sqlite_db.claim_queued_execution_jobs(limit=5)
    assert claimed["lease_epoch"] == 1
    assert sqlite_db.claim_queued_execution_jobs(limit=5) == []
    assert sqlite_db.get_execution_job("j-atomic")["lease_epoch"] == 1


def test_d05_reclaim_after_retry_rebinds_with_new_epoch(sqlite_db):
    _insert_local(sqlite_db, "j-retry")
    sqlite_db.claim_queued_execution_jobs(limit=5)
    old = job_lease.server_lease("j-retry")
    sqlite_db.update_execution_job("j-retry", status="queued")
    (again,) = sqlite_db.claim_queued_execution_jobs(limit=5)
    assert again["lease_epoch"] == 2
    row = sqlite_db.get_execution_job("j-retry")
    assert row["lease_token_hash"] != job_lease.hash_lease_token(old["token"])
    assert row["lease_token_hash"] == job_lease.hash_lease_token(job_lease.server_lease("j-retry")["token"])


def test_d05_renew_checks_token_epoch_and_expiry(sqlite_db):
    _insert_local(sqlite_db, "j-renew")
    sqlite_db.claim_queued_execution_jobs(limit=5)
    lease = job_lease.server_lease("j-renew")
    h = job_lease.hash_lease_token(lease["token"])
    wid = lease["worker_id"]
    assert sqlite_db.renew_worker_lease("j-renew", wid, "bad-hash", 1, 1800) is None
    assert sqlite_db.renew_worker_lease("j-renew", wid, h, 2, 1800) is None
    assert sqlite_db.renew_worker_lease("j-renew", "other", h, 1, 1800) is None
    assert sqlite_db.renew_worker_lease("j-renew", wid, h, "x", 1800) is None
    ok = sqlite_db.renew_worker_lease("j-renew", wid, h, 1, 3600)
    assert ok and ok["lease_epoch"] == 1
    c = sqlite_db._sqlite_conn()
    try:
        c.execute("UPDATE execution_jobs SET lease_expires_at=datetime('now','-1 seconds') WHERE id='j-renew'")
        c.commit()
    finally:
        c.close()
    assert sqlite_db.renew_worker_lease("j-renew", wid, h, 1, 1800) is None


def test_d05_ttl_and_sweeper_constants_and_actual_expiry_check():
    from billing import execution_worker as ew

    assert job_lease.LEASE_TTL_SECONDS == 1800
    assert ew.MAX_JOB_TIMEOUT_SECONDS * 2 == 1200
    job = {
        "worker_id": GPU_WORKER,
        "lease_token_hash": job_lease.hash_lease_token(TOKEN),
        "lease_epoch": 1,
        "lease_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "status": "running",
    }
    # Фактическое истечение проверяется в момент вердикта, без участия свипера.
    assert job_lease.verdict(job, worker_id=GPU_WORKER, lease_token=TOKEN, epoch=1) == (False, "expired")


@pytest.mark.pg  # G-CI-PG-CANON: env-skip без живого PG
def test_d05_pg_claim_and_bind_single_transaction(monkeypatch):
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN не задан; issue: G-CI-PG-CANON · expiry: 2026-12-31")
    import _isolation
    import psycopg2

    assert _isolation.identity(dsn)[2] in _isolation.ALLOWED_DB_NAMES
    monkeypatch.setattr(db, "_USE_PG", True)
    monkeypatch.setattr(job_lease, "_SERVER_LEASES", {})
    jid = _uniq("test-lease-pg")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT current_database()")
        assert cur.fetchone()[0] in _isolation.ALLOWED_DB_NAMES
        cur.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, backend, payload, created_at) "
            "VALUES (%s,'test-lease-pg','queued','local','{\"backend\": \"local\"}'::jsonb, "
            "TIMESTAMPTZ '1960-01-01 00:00:00+00')",
            (jid,),
        )

        def _fail():
            raise RuntimeError("bind boom")

        original = job_lease.local_worker_id
        monkeypatch.setattr(job_lease, "local_worker_id", _fail)
        with pytest.raises(RuntimeError):
            db.claim_queued_execution_jobs(limit=1)
        cur.execute("SELECT status, worker_id, lease_epoch, lease_token_hash FROM execution_jobs WHERE id=%s", (jid,))
        assert cur.fetchone() == ("queued", None, 0, None)
        monkeypatch.setattr(job_lease, "local_worker_id", original)  # повтор после ошибки

        (claimed,) = db.claim_queued_execution_jobs(limit=1)
        assert claimed["id"] == jid and "lease_token" not in claimed
        lease = job_lease.server_lease(jid)
        cur.execute(
            "SELECT status, worker_id, lease_epoch, lease_token_hash, lease_expires_at > now() + interval '1700 seconds' FROM execution_jobs WHERE id=%s",
            (jid,),
        )
        status, wid, epoch, h, ttl_ok = cur.fetchone()
        assert (status, wid, epoch) == ("running", job_lease.local_worker_id(), 1)
        assert h == job_lease.hash_lease_token(lease["token"]) and ttl_ok
        assert db.renew_worker_lease(jid, wid, h, 1, 1800)
        assert db.renew_worker_lease(jid, wid, h, 0, 1800) is None
    finally:
        cur.execute("DELETE FROM execution_jobs WHERE id=%s", (jid,))
        conn.close()


# ── D-06: worker_id обязателен для bound-задач ──────────────────────────────


def _bound_job(**over):
    job = {
        "worker_id": GPU_WORKER,
        "lease_token_hash": job_lease.hash_lease_token(TOKEN),
        "lease_epoch": 1,
        "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(),
    }
    job.update(over)
    return job


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"lease_token": TOKEN}, "no_worker_id"),
        ({"lease_token": TOKEN, "worker_id": ""}, "no_worker_id"),
        ({"lease_token": TOKEN, "worker_id": "bad id"}, "bad_worker_id"),
        ({"lease_token": TOKEN, "worker_id": 123}, "bad_worker_id"),
        ({"lease_token": TOKEN, "worker_id": "gpu-worker-2"}, "worker_mismatch"),
        ({"lease_token": "wrong", "worker_id": GPU_WORKER}, "bad_token"),
        ({"lease_token": TOKEN, "worker_id": GPU_WORKER, "epoch": 2}, "stale_epoch"),
        ({"lease_token": TOKEN, "worker_id": GPU_WORKER, "epoch": 1}, "ok"),
    ],
)
def test_d06_worker_id_required_and_does_not_replace_token_epoch(kwargs, reason):
    ok, got = job_lease.verdict(_bound_job(), **kwargs)
    assert got == reason
    assert ok is (reason == "ok")


def test_d06_enforce_worker_ack_without_worker_id_denied(world, monkeypatch):
    monkeypatch.setenv("ROMA_LEASE_ENFORCE", "1")
    jid = world["add_job"](bound=True, workers=GPU_WORKER)
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "lease_token": TOKEN},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert dict(world["audits"])["job.lease.denied"]["reason"] == "no_worker_id"
    assert world["ledger"].calls == 0
