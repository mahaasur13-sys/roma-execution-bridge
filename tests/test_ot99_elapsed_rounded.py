"""OT-99 — сумма дебета воркера не должна расходиться с хранимым duration_seconds.

На базе воркер списывал по полному `elapsed` (`time.monotonic() - start_time`),
а в `duration_seconds` писал округлённое значение. Свипер при повторе после
неопределённого COMMIT использует `duration_seconds` → сумма расходится, сверка
по ledger_id (OT-98) фейлится. Фикс: `elapsed` округляется до биллинга, хранится
то же самое значение. SQLite + in-memory ledger, без PG/сети.
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

TENANT = "tenant-ot99"
ELAPSED = 100.123456  # нецелое время — разводит полное значение и round(..., 2)


class FakeLedger:
    def __init__(self, balance=100.0):
        self.balance = balance
        self.debits: dict[str, float] = {}

    def get_tenant_balance(self, tenant_id):
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        assert idempotency_key, "finalize обязан передавать ключ"
        lid = "led-" + hashlib.sha256(f"{tenant_id}:{idempotency_key}".encode()).hexdigest()[:24]
        if lid in self.debits:
            return lid, False
        if self.balance < amount:
            return None, False
        self.balance -= amount
        self.debits[lid] = amount
        return lid, True


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

    # OT-99 остаётся про «дебет == duration_seconds»: подтверждённая длительность
    # та же, что раньше давал таймер воркера. Патчим ИСТОЧНИК (dispatcher), а не
    # обёртку воркера — тогда проверяются и валидация, и округление до 2 знаков.
    async def corroborated(job_id, backend_name=None):
        return ELAPSED

    monkeypatch.setattr("backends.dispatcher.get_confirmed_duration", corroborated)

    ticks = itertools.chain([1000.0], itertools.repeat(1000.0 + ELAPSED))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    async def fake_dispatch(**k):
        return {"backend": "local", "status": "running"}

    async def fake_status(job_id, backend_job_id):
        return {"status": "completed"}

    monkeypatch.setattr(ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": None})
    with _conn() as c:
        db._ensure_execution_jobs_table(c)
    return {"conn": _conn, "ledger": ledger}


def _row(env, job_id):
    with env["conn"]() as c:
        return dict(c.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone())


def test_worker_debit_matches_stored_duration(env):
    jid = "job-ot99"
    with env["conn"]() as c:
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'local', datetime('now', '-60 seconds'))",
            (jid, TENANT),
        )
        c.commit()

    asyncio.run(ew._execute_and_bill_impl(jid, TENANT, {"backend": "local", "task": "echo hi"}))

    row = _row(env, jid)
    lid = "led-" + hashlib.sha256(f"{TENANT}:job:{jid}".encode()).hexdigest()[:24]
    debited = env["ledger"].debits[lid]

    assert row["duration_seconds"] == round(ELAPSED, 2)
    assert debited == round(row["duration_seconds"] * 0.00001, 8), (
        f"сумма дебета {debited!r} должна совпадать с duration_seconds*rate"
    )
