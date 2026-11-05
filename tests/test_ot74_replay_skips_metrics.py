"""OT-74 — метрики инкрементятся только на НОВУЮ строку леджера (replay → skip)."""

import sys

sys.path.insert(0, "/home/felix/astrofin-sentinel-platform/.queue-work/roma-execution-bridge")

from billing import finalize as fin  # noqa: E402


class FakeLedger:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def debit_if_funds(self, *a, **k):
        self.calls += 1
        return self.result

    def get_tenant_balance(self, tid):
        return 100.0


def _fake_counter():
    return type("C", (), {"labels": lambda **k: type("Inc", (), {"inc": lambda *a, **kw: None})()})()


def test_replay_skips_metrics(monkeypatch):
    ledger = FakeLedger(("led-1", False))  # replay
    monkeypatch.setattr(fin, "billing_ledger", ledger)
    incs = []
    monkeypatch.setattr(fin, "gpu_seconds_total", type("G", (), {"labels": lambda **k: type("I", (), {"inc": lambda *a: incs.append(a)})()})())
    monkeypatch.setattr(fin, "billing_cost_total", _fake_counter())
    monkeypatch.setattr(fin, "tokens_total", _fake_counter())
    monkeypatch.setattr(fin, "job_cost", type("J", (), {"labels": lambda **k: type("O", (), {"observe": lambda *a: None})()})())
    monkeypatch.setattr(fin, "roma_debit_total", _fake_counter())
    monkeypatch.setattr(fin, "roma_debit_amount", type("A", (), {"labels": lambda **k: type("O", (), {"observe": lambda *a: None})()})())
    monkeypatch.setattr(fin, "roma_tenant_balance", type("B", (), {"labels": lambda **k: type("S", (), {"set": lambda *a: None})()})())
    monkeypatch.setattr(fin, "metering_engine", type("M", (), {"record": lambda *a, **k: None})())
    monkeypatch.setattr(fin, "_burn_tracker", {}, raising=False)

    cost, debited = fin._increment_usage("t", gpu_sec=100.0, plan_name="free", job_id="j", backend="local")
    assert debited is True and cost > 0
    assert incs == [], f"replay не должен инкрементить gpu_seconds_total: {incs}"
    assert ledger.calls == 1
