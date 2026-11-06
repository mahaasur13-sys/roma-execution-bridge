"""OT-81 — свипер продолжает после ошибки update_execution_job в ok-ветке.

Биллинг сохранён (finalize→ok), но финальный UPDATE статуса падает: свипер не
должен ронять весь батч — помечает задачу error и продолжает.
"""

import itertools
import sqlite3
import types


import db_adapter as db  # noqa: E402
from billing import execution_worker as ew  # noqa: E402

TENANT = "tenant-ot81"


def test_sweep_continues_after_complete_update_error(tmp_path, monkeypatch):
    path = tmp_path / "roma.db"

    def _conn():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(db, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db, "_sqlite_conn", _conn)
    monkeypatch.setattr(ew, "_db_adapter", db)
    monkeypatch.setattr(ew, "_no_funds_next_try", {}, raising=False)
    ticks = itertools.repeat(10_000.0)
    monkeypatch.setattr(ew, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    with _conn() as c:
        db._ensure_execution_jobs_table(c)
        # Явный completed_at: j-a раньше j-b → ORDER BY completed_at ASC
        # детерминированно выбирает j-a первой (одинаковый datetime('now')
        # не гарантирует порядок — тест «продолжения батча» был бы вакуумным).
        for jid, completed in (("j-a", "2024-01-01 00:00:00"), ("j-b", "2024-01-01 00:00:01")):
            c.execute(
                "INSERT INTO execution_jobs (id, tenant_id, status, error, cost_usd, backend, completed_at)"
                " VALUES (?, ?, 'billing_pending', '', 0.0, 'local', ?)",
                (jid, TENANT, completed),
            )
        c.commit()

    import main

    monkeypatch.setattr(main, "finalize_job_billing", lambda *a, **k: "ok")

    update_order = []

    def fake_update(jid, **kwargs):
        update_order.append(jid)
        if jid == "j-a":
            raise RuntimeError("update failed (test)")
        return 1

    monkeypatch.setattr(db, "update_execution_job", fake_update)

    out = ew.sweep_billing_pending_once(now=10_000.0)
    assert update_order == ["j-a", "j-b"], update_order  # j-a упала до j-b: батч продолжился
    assert out.get("ok") == 1, out  # j-b обработана
    assert out.get("error") == 1, out  # j-a помечена error, не уронила батч
