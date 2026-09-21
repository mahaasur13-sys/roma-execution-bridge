"""P0-2 — квота-гейт считается по TOTAL, метрика roma_jobs_active — по ACTIVE.

Разделение семантики:
  * count_jobs_for_tenant_total  → квота плана (max_jobs / max_jobs_per_month)
  * count_jobs_active_for_tenant → gauge roma_jobs_active (non-terminal)

Регресс-защита: если гейт снова начнёт считать active вместо total, тенант сможет
обойти месячную квоту, просто не превышая лимит одновременных job'ов.
"""
from __future__ import annotations

import inspect

import db_adapter as db
from cost.gate import GateResult, EnterpriseDecisionGate


class _Counters:
    """Подменяет оба счётчика и фиксирует, какой из них вызвал гейт."""

    def __init__(self, total: int, active: int):
        self.total = total
        self.active = active
        self.calls: list[str] = []

    def total_fn(self, tenant_id: str) -> int:
        self.calls.append("total")
        return self.total

    def active_fn(self, tenant_id: str) -> int:
        self.calls.append("active")
        return self.active


def _patch(monkeypatch, plan: str, total: int, active: int) -> _Counters:
    counters = _Counters(total, active)
    monkeypatch.setattr(db, "get_tenant", lambda tenant_id: {"tenant_id": tenant_id, "plan": plan})
    monkeypatch.setattr(db, "count_jobs_for_tenant_total", counters.total_fn)
    monkeypatch.setattr(db, "count_jobs_active_for_tenant", counters.active_fn)
    return counters


def test_gate_denies_on_total_not_active(monkeypatch):
    """total уже на лимите, active мал → DENIED (иначе квота обходится)."""
    counters = _patch(monkeypatch, plan="free", total=50, active=1)
    decision = EnterpriseDecisionGate().evaluate("test-quota", {"gpu_required": False})

    assert decision.result == GateResult.DENIED
    assert "quota exceeded" in decision.reason
    assert counters.calls == ["total"], f"гейт должен смотреть только total, вызвано: {counters.calls}"


def test_gate_allows_one_below_total_limit(monkeypatch):
    """total = max_jobs - 1 → ALLOWED (граница не сдвинута)."""
    _patch(monkeypatch, plan="free", total=49, active=49)
    decision = EnterpriseDecisionGate().evaluate("test-quota", {"gpu_required": False})

    assert decision.result == GateResult.ALLOWED
    assert decision.job_limit == 50
    assert decision.cost_estimated > 0


def test_gate_denies_at_total_boundary(monkeypatch):
    """Ровно на границе (total == max_jobs) → DENIED, семантика >=."""
    _patch(monkeypatch, plan="free", total=50, active=0)
    assert EnterpriseDecisionGate().evaluate("test-quota", {}).result == GateResult.DENIED


def test_enterprise_plan_is_unlimited(monkeypatch):
    """enterprise: max_jobs = -1 = безлимит; -1 не должен трактоваться как «0 остатка»."""
    _patch(monkeypatch, plan="enterprise", total=10_000, active=3)
    decision = EnterpriseDecisionGate().evaluate("test-quota", {"gpu_required": True})

    assert decision.result == GateResult.ALLOWED, decision.reason


def test_plans_loaded_from_config_dir():
    """Планы читаются из config/plans.json, а не из несуществующего plans.json."""
    plans = db._load_plans()

    assert "enterprise" in plans, "потеря плана enterprise → тенант уезжает в free-квоту"
    assert plans["enterprise"]["max_jobs"] == -1
    assert plans["free"]["max_jobs"] == 50, "config/plans.json: max_jobs_per_month=50"


def test_single_load_plans_definition():
    """Дубль _load_plans затенял первый и читал plans.json из cwd."""
    src = inspect.getsource(db)
    assert src.count("def _load_plans(") == 1


def test_single_count_jobs_definition_per_name():
    """count_jobs_for_tenant больше не переопределяется вторым определением."""
    src = inspect.getsource(db)
    # legacy-имя count_jobs_for_tenant удалено, остались два явных: total / active
    assert src.count("def count_jobs_for_tenant_total(") == 1
    assert src.count("def count_jobs_active_for_tenant(") == 1
    assert "def count_jobs_for_tenant(" not in src
    assert "def count_jobs_for_tenant_active(" not in src
