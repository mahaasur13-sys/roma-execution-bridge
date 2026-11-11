"""ACK-CAS: арендатор не запускает задачу сам, финальный UPDATE — CAS.

  a) ``action=start`` по ключу арендатора → 403 (задача остаётся queued);
  b) ``/v1/jobs/{id}/complete`` пишет completed только если статус не стал
     терминальным между чтением и UPDATE (cancel/свипер выиграли гонку) → 409;
  c) то же для финального UPDATE worker-ack;
  d) выигранная гонка даёт ровно одно итоговое состояние и не больше одного DEBIT;
  e) completed ⇒ finalize вызван ровно один раз.

Часть кейсов идёт через реальный sqlite-адаптер (без моков) — доказательство, что
CAS держит сам UPDATE, а не только ветка роутера.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import main
import router_jobs
from billing import finalize as fin
from deps import register_key

WORKER_TOKEN = "ack-cas-test-worker-token"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class CountingLedger:
    """debit_if_funds с детерминированным ключом; считает вызовы и уникальные списания."""

    def __init__(self, balance: float = 100.0):
        self.balance = balance
        self.calls = 0
        self.debits: dict[str, float] = {}

    def get_tenant_balance(self, tenant_id):
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        self.calls += 1
        lid = "led-" + hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()[:24]
        if lid in self.debits:
            return lid, False
        if self.balance < amount:
            return None, False
        self.balance -= amount
        self.debits[lid] = amount
        return lid, True


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


@pytest.fixture()
def world(monkeypatch):
    """Тенант (штатный seed), подменённое job-хранилище, spy на finalize, CountingLedger."""
    import routers.public_jobs as public_jobs

    tenant_id = _uniq("test-ackcas")
    key = _uniq("key-ackcas")
    db.seed_tenants({key: {"tenant_id": tenant_id, "name": "ACKCAS"}})
    monkeypatch.setattr(main, "is_email_verified", lambda api_key: True)
    original = dict(main.API_KEYS)
    register_key(key, {"tenant_id": tenant_id})

    jobs: dict[str, dict] = {}
    state = {"finalize_calls": 0, "race": None}

    def _get(job_id):
        j = jobs.get(job_id)
        return dict(j) if j else None

    def _update(job_id, status=None, completed_at=None, error=None, tenant_id=None, if_status_not_in=None, **kw):
        j = jobs.get(job_id)
        if not j:
            return 0
        # Гонка: параллельный cancel коммитит свой статус ровно между нашим
        # чтением и финальным UPDATE.
        if state["race"] and status == state["race"]["claim"]:
            state["race"] = None
            j["status"] = "cancelled"
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

    async def _no_confirmed(job_id, backend_name=None):
        return None

    async def _no_backend(**kw):
        return None

    real_finalize = router_jobs.finalize_job_billing

    def _finalize_spy(*a, **k):
        state["finalize_calls"] += 1
        return real_finalize(*a, **k)

    monkeypatch.setattr(router_jobs, "get_confirmed_duration", _no_confirmed)
    monkeypatch.setattr(public_jobs, "get_confirmed_duration", _no_confirmed)
    monkeypatch.setattr(public_jobs, "backend_cancel_job", _no_backend)
    monkeypatch.setattr(router_jobs, "backend_cancel_job", _no_backend)
    monkeypatch.setattr(router_jobs, "finalize_job_billing", _finalize_spy)
    monkeypatch.setattr(router_jobs, "write_audit_event", lambda *a, **k: None)
    monkeypatch.setenv("ROMA_GPU_WORKER_TOKEN", WORKER_TOKEN)

    ledger = CountingLedger()
    monkeypatch.setattr(fin, "billing_ledger", ledger)
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))

    def add_job(status="running", started_at="past"):
        jid = _uniq("job-ackcas")
        job = {"job_id": jid, "tenant_id": tenant_id, "status": status, "backend": None, "payload": {}}
        if started_at == "past":
            job["started_at"] = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        elif started_at is not None:
            job["started_at"] = started_at
        jobs[jid] = job
        return jid

    yield {
        "tenant_id": tenant_id,
        "key": key,
        "jobs": jobs,
        "ledger": ledger,
        "state": state,
        "add_job": add_job,
        "client": TestClient(main.app, raise_server_exceptions=False),
    }

    main.API_KEYS.clear()
    main.API_KEYS.update(original)


# ── a) action=start по ключу арендатора ─────────────────────────────────────


def test_tenant_start_is_forbidden_and_job_stays_queued(world):
    jid = world["add_job"](status="queued")
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "start"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 403, resp.text
    assert world["jobs"][jid]["status"] == "queued"
    assert world["ledger"].calls == 0


def test_tenant_start_cross_tenant_still_404(world):
    """Порядок проверок не меняется: чужой job → 404 раньше 403."""
    jid = world["add_job"](status="queued")
    world["jobs"][jid]["tenant_id"] = "someone-else"
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "start"},
        headers={"X-API-Key": world["key"]},
    )
    assert resp.status_code == 404


def test_worker_start_still_allowed(world):
    jid = world["add_job"](status="queued")
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "start", "worker_id": "w-1"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 200, resp.text
    assert world["jobs"][jid]["status"] == "running"


# ── b/c) CAS в финальном UPDATE ────────────────────────────────────────────


def test_complete_cas_lost_does_not_overwrite_cancel(world):
    jid = world["add_job"]()
    world["state"]["race"] = {"claim": "completed"}
    resp = world["client"].post(f"/v1/jobs/{jid}/complete", headers={"X-API-Key": world["key"]})
    assert resp.status_code == 409, resp.text
    assert "transition_denied" in resp.text
    assert world["jobs"][jid]["status"] == "cancelled"
    assert len(world["ledger"].debits) == 1, "дебет прошёл до CAS — не больше одного"


def test_worker_ack_complete_cas_lost_does_not_overwrite_cancel(world):
    jid = world["add_job"]()
    world["state"]["race"] = {"claim": "completed"}
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "complete", "worker_id": "w-1"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert world["jobs"][jid]["status"] == "cancelled"
    assert len(world["ledger"].debits) == 1


def test_worker_ack_start_cas_lost_when_cancelled_concurrently(world):
    jid = world["add_job"](status="queued")
    world["state"]["race"] = {"claim": "running"}
    resp = world["client"].post(
        f"/v1/jobs/{jid}/worker-ack",
        json={"action": "start", "worker_id": "w-1"},
        headers={"X-Roma-Worker-Token": WORKER_TOKEN},
    )
    assert resp.status_code == 409, resp.text
    assert world["jobs"][jid]["status"] == "cancelled"


def test_real_sqlite_update_respects_if_status_not_in():
    """CAS держит сам адаптер: терминальный статус не перезаписывается."""
    tenant_id = _uniq("test-ackcas-sql")
    jid = _uniq("job-ackcas-sql")
    db.insert_execution_job(job_id=jid, decision_id=_uniq("dec"), tenant_id=tenant_id, status="queued", payload={})

    assert db.update_execution_job(jid, status="running") == 1
    assert (
        db.update_execution_job(
            jid,
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            if_status_not_in=db.CANCEL_BLOCKED_STATUSES,
        )
        == 1
    )
    assert db.get_execution_job(jid)["status"] == "completed"

    # Повторный CAS по уже завершённой задаче — 0 строк и статус не меняется.
    assert db.update_execution_job(jid, status="failed", if_status_not_in=db.CANCEL_BLOCKED_STATUSES) == 0
    assert db.get_execution_job(jid)["status"] == "completed"


# ── d/e) гонка и инвариант «completed ⇒ finalize ровно раз» ────────────────


def test_cancel_then_complete_yields_single_state_and_single_debit(world):
    jid = world["add_job"]()
    c = world["client"]
    cancel = c.post(f"/v1/jobs/{jid}/cancel", headers={"X-API-Key": world["key"]})
    assert cancel.status_code == 200, cancel.text
    assert world["jobs"][jid]["status"] == "cancelled"

    complete = c.post(f"/v1/jobs/{jid}/complete", headers={"X-API-Key": world["key"]})
    assert complete.status_code == 409, complete.text
    assert world["jobs"][jid]["status"] == "cancelled"
    assert world["ledger"].calls == 0, "отмена не должна двигать деньги"
    assert world["state"]["finalize_calls"] == 0


def test_completed_implies_finalize_exactly_once(world):
    jid = world["add_job"]()
    c = world["client"]
    first = c.post(f"/v1/jobs/{jid}/complete", headers={"X-API-Key": world["key"]})
    assert first.status_code == 200, first.text
    assert world["jobs"][jid]["status"] == "completed"

    second = c.post(f"/v1/jobs/{jid}/complete", headers={"X-API-Key": world["key"]})
    assert second.status_code == 409, second.text

    assert world["state"]["finalize_calls"] == 1
    assert len(world["ledger"].debits) == 1
    assert world["jobs"][jid]["status"] == "completed"
