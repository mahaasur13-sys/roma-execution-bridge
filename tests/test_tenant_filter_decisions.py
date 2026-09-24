"""G-TENANT-FILTER-REQUIRED: tenant-изоляция read-пути decision (A/B-тенант).

Якорь (CodeRabbit Major, PR #91 id 4079650088 → косвенно): `get_decision_record(id)`
читал `decision_records` по id БЕЗ tenant-фильтра; `router_decisions.get_decision`
получал `_tenant_id`, но не сверял с `rec["tenant_id"]` → tenant B читал decision
tenant A (утечка gate_result/reason/estimated_cost/quota_remaining).

Фикс: адаптер `get_decision_record(decision_id, tenant_id)` с `WHERE id AND tenant_id`;
вызывающие согласованы (router_decisions + decision_service). Defense-in-depth:
адаптер не отдаёт чужое сам по себе, даже если маршрут фильтрует сам.

Негатив по path-инструкции CodeRabbit (прецедент T3 #93): negative authz/isolation.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402


@pytest.fixture()
def sqlite_mode(monkeypatch):
    """Форсируем SQLite-ветку (PG_DSN пуст, флаг PG сброшен)."""
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", False)
    return db


@pytest.fixture()
def decision_table(sqlite_mode):
    """decision_records в SQLite (bootstrap отсутствует — создаём явно, IF NOT EXISTS)."""
    db_path = Path(db.__file__).parent / "data" / "roma.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_records (
                id TEXT PRIMARY KEY,
                request_id TEXT,
                tenant_id TEXT,
                gate_result TEXT,
                gate_reason TEXT,
                quota_remaining REAL,
                estimated_cost REAL,
                policy_profile TEXT,
                decided_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_decision_read_is_tenant_scoped(decision_table):
    """A/B-тенант: tenant B не видит decision tenant A; tenant A видит своё."""
    did = _uniq("dec")
    tenant_a = _uniq("t-a")
    tenant_b = _uniq("t-b")

    db.insert_decision_record(
        did, _uniq("req"), tenant_a, "allowed", "", 10, 0.5, "default"
    )

    own = db.get_decision_record(did, tenant_a)
    assert own is not None
    assert own["tenant_id"] == tenant_a

    foreign = db.get_decision_record(did, tenant_b)
    assert foreign is None, "tenant B не должен читать decision tenant A"

    missing = db.get_decision_record(_uniq("dec-ghost"), tenant_a)
    assert missing is None


def test_decision_read_no_cross_tenant_overlap(decision_table):
    """Два джоба (A и B): каждый читает только своё, пересечение = 0."""
    tenant_a = _uniq("t-a")
    tenant_b = _uniq("t-b")
    id_a = _uniq("dec")
    id_b = _uniq("dec")

    db.insert_decision_record(
        id_a, _uniq("req"), tenant_a, "allowed", "", 1, 0.1, "default"
    )
    db.insert_decision_record(
        id_b, _uniq("req"), tenant_b, "allowed", "", 1, 0.1, "default"
    )

    # A читает своё, не чужое
    assert db.get_decision_record(id_a, tenant_a)["tenant_id"] == tenant_a
    assert db.get_decision_record(id_b, tenant_a) is None
    # B читает своё, не чужое
    assert db.get_decision_record(id_b, tenant_b)["tenant_id"] == tenant_b
    assert db.get_decision_record(id_a, tenant_b) is None
