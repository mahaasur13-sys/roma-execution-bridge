"""Списание только по подтверждённой бэкендом длительности (не по таймеру воркера).

Инварианты:
  a) confirmed = N < wall-clock → в finalize уходит N, в duration_seconds тоже N;
  b) confirmed = None/мусор → дебета нет, wall-clock не попадает в duration_seconds
     (свиперу нечего досписать), billing_pending не ставится;
  c) дебет прошёл, а подтверждения нет / списано больше — возврат разницы через
     refund_job без заявки клиента.

SQLite + фейковый ledger, без PG, сети, Vast и платежей.
"""

import asyncio
import hashlib
import itertools
import os
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402
from billing import finalize as fin  # noqa: E402

TENANT = "tenant-cdur"
WALL_CLOCK = 100.0  # сколько «исполнялось» по часам воркера
CONFIRMED = 40.0  # сколько подтвердил бэкенд
RATE = 0.00001


class FakeLedger:
    def __init__(self, balance=100.0):
        self.balance = balance
        self.debits: dict[str, float] = {}
        self.refunds: list[tuple] = []

    def get_tenant_balance(self, tenant_id):
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        lid = "led-" + hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()[:24]
        if lid in self.debits:
            return lid, False
        if self.balance < amount:
            return None, False
        self.balance -= amount
        self.debits[lid] = amount
        return lid, True

    def refund(self, tenant_id, amount, currency="USD", job_id=None, reason="refund"):
        self.refunds.append((job_id, amount, reason))
        return "led-refund"


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
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: (None, True))
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)

    ticks = itertools.chain([1000.0], itertools.repeat(1000.0 + WALL_CLOCK))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    async def fake_dispatch(**k):
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
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'local', datetime('now', '-60 seconds'))",
            ("job-1", TENANT),
        )
        c.commit()
    return {"conn": _conn, "ledger": ledger}


def _backend_reports(monkeypatch, value):
    async def fake(job_id, backend_name=None):
        return value

    monkeypatch.setattr("backends.dispatcher.get_confirmed_duration", fake)


def _row(env, job_id="job-1"):
    with env["conn"]() as c:
        return dict(c.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone())


def _run(job_id="job-1"):
    return asyncio.run(ew._execute_and_bill_impl(job_id, TENANT, {"backend": "local", "task": "echo hi"}))


def test_bills_confirmed_not_wall_clock(env, monkeypatch):
    """(a) confirmed < elapsed → дебет и duration_seconds считаются от confirmed."""
    _backend_reports(monkeypatch, CONFIRMED)

    out = _run()
    row = _row(env)

    lid = "led-" + hashlib.sha256(f"{TENANT}:job:job-1".encode()).hexdigest()[:24]
    assert env["ledger"].debits[lid] == pytest.approx(CONFIRMED * RATE)
    assert row["duration_seconds"] == pytest.approx(CONFIRMED)
    assert row["cost_usd"] == pytest.approx(CONFIRMED * RATE)
    assert row["duration_seconds"] != pytest.approx(WALL_CLOCK)
    assert out["duration_seconds"] == pytest.approx(CONFIRMED)


@pytest.mark.parametrize("value", [None, "abc", -1.0, float("nan"), float("inf")])
def test_no_confirmation_means_no_debit(env, monkeypatch, value):
    """(b) нет/мусор в подтверждении → дебета нет и wall-clock не пишется в duration_seconds."""
    _backend_reports(monkeypatch, value)

    _run()
    row = _row(env)

    assert env["ledger"].debits == {}
    assert row["duration_seconds"] in (0, 0.0, None)
    assert row["cost_usd"] in (0, 0.0)
    assert row["error"] == "billing:unconfirmed"
    # Свиперу нечего досписать: ни billing_pending, ни wall-clock в duration_seconds.
    assert row["status"] == "completed"


def test_refund_when_no_confirmation_after_debit(env, monkeypatch):
    """(c) дебет прошёл, подтверждения нет → возврат всей суммы через refund_job."""
    import main

    monkeypatch.setattr(main, "refund_job", lambda *a, **k: env["ledger"].refund(*a, **k))

    amount = asyncio.run(ew._refund_if_overbilled("job-1", TENANT, WALL_CLOCK, None))

    assert amount == pytest.approx(WALL_CLOCK * RATE)
    assert env["ledger"].refunds == [("job-1", pytest.approx(WALL_CLOCK * RATE), "unconfirmed_duration")]


def test_refund_only_the_difference(env, monkeypatch):
    """(c) списано больше подтверждённого → возвращается только разница."""
    import main

    monkeypatch.setattr(main, "refund_job", lambda *a, **k: env["ledger"].refund(*a, **k))

    amount = asyncio.run(ew._refund_if_overbilled("job-1", TENANT, WALL_CLOCK, CONFIRMED))

    assert amount == pytest.approx((WALL_CLOCK - CONFIRMED) * RATE)
    assert env["ledger"].refunds[0][1] == pytest.approx((WALL_CLOCK - CONFIRMED) * RATE)


def test_no_refund_when_charged_equals_confirmed(env, monkeypatch):
    """(c) списано ровно подтверждённое → возврата нет."""
    import main

    monkeypatch.setattr(main, "refund_job", lambda *a, **k: env["ledger"].refund(*a, **k))

    amount = asyncio.run(ew._refund_if_overbilled("job-1", TENANT, CONFIRMED, CONFIRMED))

    assert amount == 0.0
    assert env["ledger"].refunds == []


def test_refund_failure_marks_pending(env, monkeypatch):
    """(c) возврат не удался → durable-маркер refund_pending (как C10), не молча."""
    import main

    def boom(*a, **k):
        raise RuntimeError("refund PG down (test)")

    monkeypatch.setattr(main, "refund_job", boom)
    monkeypatch.setattr(ew, "asyncio", types.SimpleNamespace(sleep=lambda *a, **k: asyncio.sleep(0)))

    asyncio.run(ew._refund_if_overbilled("job-1", TENANT, WALL_CLOCK, None))

    assert _row(env)["error"].startswith("refund_pending:debit_exists:")
