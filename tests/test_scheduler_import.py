"""P1: scheduler/roma_scheduler.py must import (no missing DecisionGate)."""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_roma_scheduler_imports_without_error():
    mod = importlib.import_module("scheduler.roma_scheduler")
    assert hasattr(mod, "ROMAGPUScheduler")
    assert hasattr(mod, "ROMAJobExecutor")


def test_decision_gate_alias_resolves_to_enterprise_gate():
    from cost.gate import EnterpriseDecisionGate
    import scheduler.roma_scheduler as scheduler

    # The alias must point at the real class, not a made-up DecisionGate.
    assert scheduler.DecisionGate is EnterpriseDecisionGate
