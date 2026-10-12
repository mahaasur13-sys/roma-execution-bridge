"""PA-02: the spend-cap guard is wired into all three job-creation paths.

For each path, cap=0 and PG-unavailable (unknown balance) must NOT create a job.
"""

import sys
import types
import asyncio

if "pydantic_settings" not in sys.modules:
    _fake = types.ModuleType("pydantic_settings")

    class _BaseSettings:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    _fake.BaseSettings = _BaseSettings
    sys.modules["pydantic_settings"] = _fake

import pytest
from fastapi import HTTPException

import db_adapter
import decision_store
import main
from billing.pg_ledger import PGUnavailableError
from models.app import RomaTaskInput


class _FakeLedger:
    def __init__(self, persistent=True, balance=0.0, raises=False):
        self._p = persistent
        self._b = balance
        self._r = raises

    @property
    def is_persistent(self) -> bool:
        return self._p

    def get_tenant_balance(self, tenant_id):
        if self._r:
            raise PGUnavailableError("pg down")
        return self._b


def _set_guard(monkeypatch, cap=5.0, ledger=None):
    monkeypatch.setattr(
        main,
        "_plan_limits_or_refusal",
        lambda p: ({"spend_cap_usd": cap, "max_jobs_per_month": -1}, None),
    )
    monkeypatch.setattr(main, "billing_ledger", ledger or _FakeLedger())
    monkeypatch.setattr(main, "track_spend_cap", lambda *a, **k: None)
    monkeypatch.setattr(main, "track_spend_cap_blocked", lambda *a, **k: None)
    monkeypatch.setattr(
        main, "alert_dispatcher", types.SimpleNamespace(send=lambda *a, **k: None)
    )


def _active_tenant(tid="t1"):
    return {"tenant_id": tid, "subscription_status": "active", "plan": "free"}


# ── submit_atom_cluster ────────────────────────────────────────

def _cluster_spec(nodes=2, expected_gpu_seconds=100):
    return {"nodes": nodes, "expected_gpu_seconds": expected_gpu_seconds}


def test_cluster_cap_zero_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=0.0)
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: _active_tenant())
    created = []
    monkeypatch.setattr(db_adapter, "insert_job_raw", lambda *a, **k: created.append(1))
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            main.submit_atom_cluster(
                {"cluster_spec": _cluster_spec()}, {"tenant_id": "t1"}
            )
        )
    assert e.value.status_code == 402
    assert created == []


def test_cluster_pg_unavailable_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=5.0, ledger=_FakeLedger(persistent=False))
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: _active_tenant())
    created = []
    monkeypatch.setattr(db_adapter, "insert_job_raw", lambda *a, **k: created.append(1))
    with pytest.raises(HTTPException):
        asyncio.run(
            main.submit_atom_cluster(
                {"cluster_spec": _cluster_spec()}, {"tenant_id": "t1"}
            )
        )
    assert created == []


def test_cluster_missing_spec_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=5.0)
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: _active_tenant())
    created = []
    monkeypatch.setattr(db_adapter, "insert_job_raw", lambda *a, **k: created.append(1))
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.submit_atom_cluster({"cluster_spec": {}}, {"tenant_id": "t1"}))
    assert e.value.status_code == 422
    assert created == []


def test_cluster_nodes_below_one_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=5.0)
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: _active_tenant())
    created = []
    monkeypatch.setattr(db_adapter, "insert_job_raw", lambda *a, **k: created.append(1))
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            main.submit_atom_cluster(
                {"cluster_spec": {"nodes": 0, "expected_gpu_seconds": 100}},
                {"tenant_id": "t1"},
            )
        )
    assert e.value.status_code == 422
    assert created == []


# ── submit_job ─────────────────────────────────────────────────

def test_submit_job_cap_zero_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=0.0)
    monkeypatch.setattr(main, "_check_limits", lambda tid: (True, ""))
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: _active_tenant())
    monkeypatch.setattr(main, "jobs", {})
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.submit_job(RomaTaskInput(task="x"), {"tenant_id": "t1"}))
    assert e.value.status_code == 402
    assert main.jobs == {}


def test_submit_job_pg_unavailable_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=5.0, ledger=_FakeLedger(persistent=False))
    monkeypatch.setattr(main, "_check_limits", lambda tid: (True, ""))
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: _active_tenant())
    monkeypatch.setattr(main, "jobs", {})
    with pytest.raises(HTTPException):
        asyncio.run(main.submit_job(RomaTaskInput(task="x"), {"tenant_id": "t1"}))
    assert main.jobs == {}


# ── decision_store.submit_job_through_gate ─────────────────────

def _setup_gate(monkeypatch):
    monkeypatch.setattr(db_adapter, "insert_decision_request", lambda *a, **k: None)
    monkeypatch.setattr(db_adapter, "find_decision_by_idempotency", lambda *a, **k: None)
    monkeypatch.setattr(db_adapter, "get_tenant", lambda tid: _active_tenant())
    monkeypatch.setattr(db_adapter, "count_jobs_by_tenant", lambda tid: 0)
    monkeypatch.setattr(decision_store, "_get_plan_limit", lambda p: 10)
    monkeypatch.setattr(db_adapter, "insert_decision_record", lambda *a, **k: None)
    import audit.event_store as aes

    monkeypatch.setattr(aes, "on_decision_denied", lambda *a, **k: None)
    monkeypatch.setattr(aes, "on_decision_allowed", lambda *a, **k: None)
    monkeypatch.setattr(aes, "on_job_created", lambda *a, **k: None)


def test_gate_cap_zero_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=0.0)
    _setup_gate(monkeypatch)
    created = []
    monkeypatch.setattr(db_adapter, "insert_job", lambda *a, **k: created.append(1))
    with pytest.raises(HTTPException) as e:
        decision_store.submit_job_through_gate("t1", "gpu_job", {"gpu": 1})
    assert e.value.status_code == 402
    assert created == []


def test_gate_pg_unavailable_does_not_create_job(monkeypatch):
    _set_guard(monkeypatch, cap=5.0, ledger=_FakeLedger(persistent=False))
    _setup_gate(monkeypatch)
    created = []
    monkeypatch.setattr(db_adapter, "insert_job", lambda *a, **k: created.append(1))
    with pytest.raises(HTTPException):
        decision_store.submit_job_through_gate("t1", "gpu_job", {"gpu": 1})
    assert created == []
