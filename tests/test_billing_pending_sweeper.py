"""Свипер billing_pending: задание выполнено, дебет не сохранился → повторный
finalize с тем же job_id (ключ job:<id>), без повторного исполнения и без
двойного DEBIT. SQLite-ветка db_adapter на tmp_path + in-memory ledger (без PG,
без реальных backend'ов — только моки)."""

import asyncio
import hashlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db
from billing import execution_worker as ew
from billing import finalize as fin
from billing.pg_ledger import PGUnavailableError

TENANT = "tenant-bp"


class FakeLedger:
    """Семантика debit_if_funds: детерминированный ledger_id по ключу, ON CONFLICT DO NOTHING."""

    def __init__(self, balance=100.0):
        self.balance = balance
        self.pg_down = False
        self.debits: dict[str, float] = {}
        self.calls = 0

    def get_tenant_balance(self, tenant_id):
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        self.calls += 1
        if self.pg_down:
            raise PGUnavailableError("pg down (test)")
        assert idempotency_key, "finalize обязан передавать ключ"
        lid = "led-" + hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()[:24]
        if lid in self.debits:
            return lid
        if self.balance < amount:
            return None
        self.balance -= amount
        self.debits[lid] = amount
        return lid


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    ledger = FakeLedger()
    monkeypatch.setattr(fin, "billing_ledger", ledger)
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: None)
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)

    dispatched = []

    async def fake_dispatch(**k):
        dispatched.append(k["job_id"])
        return {"backend": "local", "status": "running"}

    async def fake_status(job_id, backend_job_id):
        return {"status": "completed"}

    async def fake_cancel(job_id, tenant_id):
        return None

    monkeypatch.setattr(
        ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": fake_cancel}
    )
    with _conn() as c:
        db._ensure_execution_jobs_table(c)
    return {"conn": _conn, "ledger": ledger, "dispatched": dispatched}


def _insert(env, job_id, status, duration=100.0, error=""):
    with env["conn"]() as c:
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, duration_seconds, error, cost_usd, backend)"
            " VALUES (?, ?, ?, ?, ?, 0.0, 'local')",
            (job_id, TENANT, status, duration, error),
        )
        c.commit()


def _row(env, job_id):
    with env["conn"]() as c:
        return dict(c.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone())


def test_pg_error_in_finalize_leaves_billing_pending_cost_zero(env):
    _insert(env, "j1", "running")
    env["ledger"].pg_down = True
    asyncio.run(ew._execute_and_bill_impl("j1", TENANT, {"task": "echo hi", "backend": "local"}))
    row = _row(env, "j1")
    assert row["status"] == "billing_pending"
    assert row["cost_usd"] == 0.0
    assert row["error"] == "billing:pg_error"
    assert env["ledger"].debits == {}


def test_sweeper_bills_once_and_completes(env):
    _insert(env, "j2", "billing_pending", duration=100.0, error="billing:pg_error")
    out = ew.sweep_billing_pending_once()
    assert out["ok"] == 1
    row = _row(env, "j2")
    assert row["status"] == "completed"
    assert row["cost_usd"] == pytest.approx(100.0 * 0.00001)
    assert row["error"] in ("", None)
    assert len(env["ledger"].debits) == 1
    assert env["dispatched"] == [], "задание не должно исполняться заново"


def test_repeat_sweep_adds_no_debit(env):
    _insert(env, "j3", "billing_pending", error="billing:pg_error")
    ew.sweep_billing_pending_once()
    before = dict(env["ledger"].debits)
    assert _row(env, "j3")["status"] == "completed"
    out = ew.sweep_billing_pending_once()
    assert out["ok"] == 0
    assert env["ledger"].debits == before and len(before) == 1


def test_debit_saved_but_status_lost_is_not_debited_twice(env):
    # дебет уже прошёл (ошибка случилась после) — повтор возвращает тот же ledger_id
    _insert(env, "j4", "billing_pending", error="billing:error")
    env["ledger"].debits["led-" + hashlib.sha256(f"{TENANT}:job:j4".encode()).hexdigest()[:24]] = 0.001
    bal = env["ledger"].balance
    ew.sweep_billing_pending_once()
    assert _row(env, "j4")["status"] == "completed"
    assert env["ledger"].balance == bal and len(env["ledger"].debits) == 1


def test_sweeper_pg_down_keeps_pending_and_does_not_raise(env):
    _insert(env, "j5", "billing_pending", error="billing:pg_error")
    env["ledger"].pg_down = True
    out = ew.sweep_billing_pending_once()
    assert out["pg_error"] == 1
    row = _row(env, "j5")
    assert row["status"] == "billing_pending" and row["cost_usd"] == 0.0
    env["ledger"].pg_down = False
    ew.sweep_billing_pending_once()
    assert _row(env, "j5")["status"] == "completed"
    assert len(env["ledger"].debits) == 1


def test_no_funds_stays_pending_backoff_no_reexecution(env):
    _insert(env, "j6", "billing_pending", error="billing:pg_error")
    env["ledger"].balance = 0.0
    out = ew.sweep_billing_pending_once(now=1000.0)
    assert out["no_funds"] == 1
    row = _row(env, "j6")
    assert row["status"] == "billing_pending" and row["error"] == "billing:no_funds"
    calls = env["ledger"].calls
    out = ew.sweep_billing_pending_once(now=1060.0)
    assert out["deferred"] == 1 and env["ledger"].calls == calls, "no_funds не должен долбить ledger"
    env["ledger"].balance = 10.0
    out = ew.sweep_billing_pending_once(now=1000.0 + ew.BILLING_PENDING_NO_FUNDS_RETRY_S + 1)
    assert out["ok"] == 1 and _row(env, "j6")["status"] == "completed"
    assert env["dispatched"] == []
    assert len(env["ledger"].debits) == 1


def test_batch_limit(env, monkeypatch):
    monkeypatch.setattr(ew, "BILLING_PENDING_BATCH", 2)
    for i in range(3):
        _insert(env, f"b{i}", "billing_pending")
    out = ew.sweep_billing_pending_once()
    assert out["ok"] == 2
    assert db.count_billing_pending_jobs() == 1


# --- P1: клиентская отмена не должна уводить billing_pending из-под свипера ---

from fastapi import HTTPException  # noqa: E402

import router_jobs  # noqa: E402
from routers import public_jobs  # noqa: E402

KEY = {"tenant_id": TENANT}


@pytest.fixture
def cancel_env(env, monkeypatch):
    cleaned = []

    async def fake_backend_cancel(tenant_id, job_id):
        cleaned.append(job_id)

    monkeypatch.setattr(public_jobs, "backend_cancel_job", fake_backend_cancel)
    monkeypatch.setattr(public_jobs, "write_event", lambda *a, **k: None)
    monkeypatch.setattr(router_jobs, "_tenant_from_key", lambda key=None: TENANT)
    monkeypatch.setattr(router_jobs, "evaluate_policies", lambda *a, **k: {"result": "allowed"})
    monkeypatch.setattr(router_jobs, "write_audit_event", lambda *a, **k: None)
    env["cleaned"] = cleaned
    return env


def _public_cancel(job_id):
    return asyncio.run(public_jobs.cancel_job(job_id, key_info=KEY))


def test_public_cancel_billing_pending_409_then_sweeper_debits_once(cancel_env):
    _insert(cancel_env, "c1", "billing_pending", error="billing:pg_error")
    with pytest.raises(HTTPException) as ei:
        _public_cancel("c1")
    assert ei.value.status_code == 409
    assert "billing_pending" in str(ei.value.detail)
    assert _row(cancel_env, "c1")["status"] == "billing_pending"
    assert cancel_env["cleaned"] == []
    ew.sweep_billing_pending_once()
    assert _row(cancel_env, "c1")["status"] == "completed"
    assert len(cancel_env["ledger"].debits) == 1


@pytest.mark.parametrize("status", ["completed", "timeout", "failed", "cancelled", "error"])
def test_public_cancel_terminal_409_status_unchanged(cancel_env, status):
    _insert(cancel_env, "c2", status)
    with pytest.raises(HTTPException) as ei:
        _public_cancel("c2")
    assert ei.value.status_code == 409
    assert _row(cancel_env, "c2")["status"] == status


@pytest.mark.parametrize("status", ["queued", "running"])
def test_public_cancel_active_still_works(cancel_env, status):
    _insert(cancel_env, "c3", status)
    assert _public_cancel("c3") == {"status": "cancelled", "job_id": "c3"}
    assert _row(cancel_env, "c3")["status"] == "cancelled"
    assert cancel_env["cleaned"] == ["c3"]


def test_v1_cancel_race_running_to_billing_pending_409(cancel_env, monkeypatch):
    # Проверка видит running, но к моменту записи воркер уже поставил billing_pending.
    _insert(cancel_env, "c4", "billing_pending", error="billing:pg_error")
    monkeypatch.setattr(
        router_jobs.db, "get_execution_job", lambda jid: {"id": jid, "tenant_id": TENANT, "status": "running"}
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(router_jobs.cancel_job("c4", x_api_key="k"))
    assert ei.value.status_code == 409
    assert _row(cancel_env, "c4")["status"] == "billing_pending"


def test_v1_cancel_running_still_works(cancel_env):
    _insert(cancel_env, "c5", "running")
    assert asyncio.run(router_jobs.cancel_job("c5", x_api_key="k"))["status"] == "cancelled"
    assert _row(cancel_env, "c5")["status"] == "cancelled"
