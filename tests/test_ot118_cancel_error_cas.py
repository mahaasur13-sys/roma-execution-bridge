"""OT-118 — CancelledError-обработчик воркера не должен перезаписывать статус без CAS.

Гипотеза (open_tasks :118): обработчик `except asyncio.CancelledError` в
`execute_and_bill` пишет `status="cancelled"` без `if_status_not_in`. Если задача
уже прошла финальный UPDATE (например, записала `billing_pending` из-за сбоя
биллинга) и корутина отменяется на шаге cleanup (vastai), обработчик стирает
`billing_pending` → задача уходит из-под свипера и работа не списывается.

Воспроизведение: vastai-бэкенд с contract_id (чтобы после финального UPDATE был
`await` cleanup), биллинг фейлится (`finalize_job_billing` → "pg_error"), а
`_backend_manager["cancel"]` поднимает CancelledError. Ожидаем: статус остаётся
`billing_pending`, ошибка `billing:pg_error`. SQLite-ветка db_adapter + моки,
без PG, без сети.
"""

import asyncio
import itertools
import os
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402

TENANT = "tenant-ot118"


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
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    # Фиктивные монотонные часы: start=1000, elapsed=100.
    ticks = itertools.chain([1000.0], itertools.repeat(1100.0))
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    with _conn() as c:
        db._ensure_execution_jobs_table(c)
    return {"conn": _conn}


def _insert_running(env, job_id):
    with env["conn"]() as c:
        c.execute(
            "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, started_at)"
            " VALUES (?, ?, 'running', '', 0.0, 'vastai', datetime('now', '-60 seconds'))",
            (job_id, TENANT),
        )
        c.commit()


def _row(env, job_id):
    with env["conn"]() as c:
        return dict(c.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone())


def test_cancel_after_final_update_keeps_billing_pending(env, monkeypatch):
    _insert_running(env, "ot118")

    import main

    monkeypatch.setattr(main, "finalize_job_billing", lambda *a, **k: "pg_error")

    async def fake_dispatch(**k):
        return {
            "backend": "vastai",
            "status": "running",
            "contract_id": "c-1",
            "price_per_hour": 0.0,
            "backend_job_id": "bj-1",
        }

    async def fake_status(job_id, backend_job_id):
        return {"status": "completed"}

    cancel_calls = {"n": 0}

    async def fake_cancel(job_id, tenant_id):
        cancel_calls["n"] += 1
        if cancel_calls["n"] == 1:
            raise asyncio.CancelledError()
        return None

    monkeypatch.setattr(
        ew, "_backend_manager", {"dispatch": fake_dispatch, "status": fake_status, "cancel": fake_cancel}
    )

    async def run():
        try:
            await ew.execute_and_bill("ot118", TENANT, {"task": "echo hi", "backend": "vastai"})
        except asyncio.CancelledError:
            pass

    asyncio.run(run())

    row = _row(env, "ot118")
    assert row["status"] == "billing_pending", (
        f"CancelledError-обработчик перезаписал billing_pending → {row['status']!r}"
    )
    assert row["error"] == "billing:pg_error", f"ошибка биллинга затерта → {row['error']!r}"
    assert cancel_calls["n"] == 2, "cleanup (шаг 6) и повторный cancel в обработчике должны сработать"
