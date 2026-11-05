"""G5 / open_tasks :94 + :116 — гонка финального UPDATE воркера.

Воркер (_execute_and_bill_impl) после исполнения пишет итоговый статус в
execution_jobs. Если за время исполнения задачу отменил (/cancel) или закрыл
(/complete) арендатор, финальная запись не должна перезаписывать этот статус:
иначе отменённая задача уходит в billing_pending и её списывает свипер, а
закрытая через /complete — получает billing_pending/billing:skip поверх
completed. SQLite-ветка db_adapter на tmp_path + in-memory ledger, без PG,
без docker, без реальных backend'ов.
"""

import asyncio
import hashlib
import itertools
import logging
import os
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402
from billing import finalize as fin  # noqa: E402
from routers import public_jobs  # noqa: E402

TENANT = "tenant-g5"
KEY = {"tenant_id": TENANT}
WORKER_SECONDS = 100.0  # воркер "исполнял" задачу 100 с (фиктивные часы)
COMPLETE_SECONDS = 50.0  # /complete видит подтверждённые 50 с


class FakeLedger:
    """Семантика debit_if_funds: детерминированный ledger_id по ключу, ON CONFLICT DO NOTHING."""

    def __init__(self, balance=100.0):
        self.balance = balance
        self.debits: dict[str, float] = {}
        self.calls = 0

    def get_tenant_balance(self, tenant_id):
        return self.balance

    def debit_if_funds(self, tenant_id, amount, currency="USD", idempotency_key=None, **meta):
        self.calls += 1
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
    records = []
    monkeypatch.setattr(fin.metering_engine, "record", lambda **k: records.append(k))
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)

    # Фиктивные монотонные часы только для модуля воркера: start=1000, далее 1000+100.
    ticks = itertools.chain([1000.0], itertools.repeat(1000.0 + WORKER_SECONDS))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    async def fake_backend_cancel(tenant_id, job_id):
        return None

    async def fake_confirmed(job_id, backend):
        return COMPLETE_SECONDS

    monkeypatch.setattr(public_jobs, "backend_cancel_job", fake_backend_cancel)
    monkeypatch.setattr(public_jobs, "get_confirmed_duration", fake_confirmed)
    monkeypatch.setattr(public_jobs, "write_event", lambda *a, **k: None)
    monkeypatch.setattr(public_jobs.db, "get_tenant", lambda tid: {"plan": "free"})

    state = {"interleave": None}

    async def fake_dispatch(**k):
        return {"backend": "local", "status": "running"}

    async def fake_status(job_id, backend_job_id):
        # Пока воркер "исполняет" задачу, арендатор вызывает /cancel или /complete.
        hook = state.pop("interleave", None)
        if hook is not None:
            await hook(job_id)
        return {"status": "completed"}

    async def fake_cancel(job_id, tenant_id):
        return None

    monkeypatch.setattr(
        ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": fake_cancel}
    )
    with _conn() as c:
        db._ensure_execution_jobs_table(c)
    return {"conn": _conn, "ledger": ledger, "records": records, "state": state}


def _insert_running(env, job_id):
    # Как после claim_queued_execution_jobs: running + started_at.
    with env["conn"]() as c:
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'local', datetime('now', '-60 seconds'))",
            (job_id, TENANT),
        )
        c.commit()


def _row(env, job_id):
    with env["conn"]() as c:
        return dict(c.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone())


def _run_worker(job_id):
    return asyncio.run(ew._execute_and_bill_impl(job_id, TENANT, {"task": "echo hi", "backend": "local"}))


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_g5a_cancel_during_execution_no_billing_pending_no_debit(env):
    """(a) :116 — отмена во время исполнения → нет billing_pending и нет DEBIT от свипера."""
    _insert_running(env, "g5a")

    async def tenant_cancels(job_id):
        assert await public_jobs.cancel_job(job_id, key_info=KEY) == {"status": "cancelled", "job_id": job_id}

    env["state"]["interleave"] = tenant_cancels
    # Свой handler: логгер roma.* может не пропагировать в caplog (JSON-логирование).
    handler = _ListHandler()
    wlog = logging.getLogger("roma.execution_worker")
    wlog.addHandler(handler)
    try:
        _run_worker("g5a")
    finally:
        wlog.removeHandler(handler)

    row = _row(env, "g5a")
    assert row["status"] == "cancelled", f"финальный UPDATE воркера перезаписал cancelled → {row['status']!r}"
    assert row["error"] != "billing:skip"
    assert env["ledger"].debits == {}

    out = ew.sweep_billing_pending_once(now=10_000.0)
    assert out["ok"] == 0
    assert env["ledger"].debits == {}, "свипер списал отменённую задачу"
    assert _row(env, "g5a")["status"] == "cancelled"
    assert any("final_update_skipped" in m for m in handler.messages), "0 строк должно логироваться"


def test_g5b_complete_during_execution_no_overwrite_single_debit(env):
    """(b) :94 — /complete во время исполнения → статус не перезаписан, ровно один DEBIT, без повторного учёта."""
    _insert_running(env, "g5b")

    async def tenant_completes(job_id):
        assert await public_jobs.complete_job(job_id, key_info=KEY) == {"status": "completed", "job_id": job_id}

    env["state"]["interleave"] = tenant_completes
    _run_worker("g5b")

    row = _row(env, "g5b")
    assert row["status"] == "completed", f"финальный UPDATE воркера перезаписал completed → {row['status']!r}"
    assert row["error"] != "billing:skip"
    assert list(env["ledger"].debits.values()) == [pytest.approx(COMPLETE_SECONDS * 0.00001)]

    out = ew.sweep_billing_pending_once(now=10_000.0)
    assert out["ok"] == 0, "задача не должна попадать в свипер после /complete"
    assert len(env["ledger"].debits) == 1, "двойной DEBIT"
    assert len(env["records"]) == 1, "usage учтён повторно (metering_engine.record вызван ещё раз)"
    assert _row(env, "g5b")["status"] == "completed"


def test_g5_no_race_worker_still_bills_and_completes(env):
    """Контроль: без вмешательства арендатора воркер списывает и закрывает задачу как раньше."""
    _insert_running(env, "g5c")
    _run_worker("g5c")
    row = _row(env, "g5c")
    assert row["status"] == "completed"
    assert row["cost_usd"] == pytest.approx(WORKER_SECONDS * 0.00001)
    assert list(env["ledger"].debits.values()) == [pytest.approx(WORKER_SECONDS * 0.00001)]


@pytest.mark.xfail(
    strict=True,
    reason="issue: G5-116-residual · expiry: 2026-12-31 · остаточное окно G5: /cancel между дебетом "
    "воркера (finalize ok) и финальным UPDATE. CAS в финальном UPDATE его не закрывает; "
    "нужен отдельный статус finalizing / CAS до finalize.",
)
def test_g5d_cancel_between_finalize_and_final_update_residual(env, monkeypatch):
    """Документирует окно, которое CAS в финальном UPDATE не закрывает (инвариант: DEBIT ⇒ completed)."""
    _insert_running(env, "g5d")
    import main

    real = fin.finalize_job_billing

    def finalize_then_cancel(*a, **k):
        res = real(*a, **k)
        # атомарная часть /cancel (тот же UPDATE, что в public_jobs.cancel_job)
        db.update_execution_job("g5d", status="cancelled", tenant_id=TENANT, if_status_not_in=db.CANCEL_BLOCKED_STATUSES)
        return res

    monkeypatch.setattr(main, "finalize_job_billing", finalize_then_cancel)
    _run_worker("g5d")
    row = _row(env, "g5d")
    assert len(env["ledger"].debits) == 1
    assert row["status"] == "completed", f"есть DEBIT, но статус {row['status']!r}"


def test_g5_final_write_blocked_matches_cancel_blocked():
    """Набор CAS финальной записи воркера не расходится с db_adapter.CANCEL_BLOCKED_STATUSES."""
    assert set(ew._FINAL_WRITE_BLOCKED_STATUSES) == set(db.CANCEL_BLOCKED_STATUSES)
