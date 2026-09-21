"""P0-2 — sqlite-путь не должен читать rowcount у Connection.

`sqlite3.Connection` не имеет `.rowcount`: `c.rowcount` бросает AttributeError, из-за
чего `POST /submit` (idempotency-резерв) и `_update_execution_job_sqlite` отдавали 500
вместо 202 на SQLite-fallback. Регресс-защита ниже проверяет оба места напрямую.
"""

from __future__ import annotations

import inspect
import os
import re
import sqlite3
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402


@pytest.fixture()
def sqlite_mode(monkeypatch):
    """Форсируем SQLite-ветку: PG_DSN пуст, флаг PG сброшен."""
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", False)
    return db


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_create_job_idempotency_sqlite_returns_true_then_false(sqlite_mode):
    tenant_id = _uniq("test-rowcount")
    key = _uniq("key")

    assert db.create_job_idempotency(tenant_id, key, "job-1") is True
    # Ключ уже занят — второй вызов не должен создавать маппинг и не должен падать.
    assert db.create_job_idempotency(tenant_id, key, "job-2") is False
    assert db.find_job_by_idempotency(tenant_id, key) == "job-1"


def test_update_execution_job_sqlite_rowcount(sqlite_mode):
    job_id = _uniq("job-upd")
    tenant_id = _uniq("test-upd")
    db.insert_execution_job(job_id, "d-1", tenant_id, "queued", {"task": "echo hi"})

    n = db._update_execution_job_sqlite(
        job_id, "running", None, None, "local", None, None, None
    )
    assert n == 1

    # Повторный перевод в тот же статус через guard — 0 строк, но без исключения.
    n_same = db._update_execution_job_sqlite(
        job_id, "running", None, None, "local", None, None, None,
        if_status_not_in=["running"],
    )
    assert n_same == 0


def test_no_rowcount_on_sqlite_connection_in_source():
    """Статический guard: `c.rowcount` у connection-объекта в db_adapter.py не осталось."""
    src = inspect.getsource(db)
    # `cur.rowcount` / `cursor.rowcount` разрешены, голый `c.rowcount` — нет.
    offenders = re.findall(r"(?<!\.)\bc\.rowcount\b", src)
    assert offenders == [], f"connection-level rowcount в db_adapter.py: {offenders}"


def test_sqlite_connection_has_no_rowcount():
    """Проверка предпосылки: у Connection действительно нет rowcount."""
    conn = sqlite3.connect(":memory:")
    try:
        assert not hasattr(conn, "rowcount")
    finally:
        conn.close()
