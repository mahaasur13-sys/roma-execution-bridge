#!/usr/bin/env python3
"""Порог сверки из env, обновление агрегатных gauge в loop, rc скрипта сверки."""

import importlib.util
import os
import sys

sys.path.insert(0, ".")

import main
from billing.pg_ledger import (
    DRIFT_THRESHOLD_DEFAULT,
    PGBillingLedger,
    drift_threshold_from_env,
)
from monitoring.metrics import (
    roma_ledger_entry_count_total,
    roma_ledger_reconcile_last_success_timestamp_seconds,
    roma_ledger_reconciliation_diff_max,
    roma_ledger_tenants_drifted,
    roma_spend_cap_exceeded_tenants,
)

SUMMARY = {
    "diff_max": 1.25,
    "drifted": 2,
    "entries_total": 309,
    "drifted_top": [{"tenant_id": "t-a", "diff": 1.25}],
    "spend_cap_exceeded": 1,
    "threshold_usd": 0.05,
}


def _script_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "reconciliation.py")
    spec = importlib.util.spec_from_file_location("roma_reconciliation_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_threshold_default_when_unset(monkeypatch):
    monkeypatch.delenv("ROMA_LEDGER_DRIFT_THRESHOLD_USD", raising=False)
    assert drift_threshold_from_env() == DRIFT_THRESHOLD_DEFAULT


def test_threshold_parsed_when_valid(monkeypatch):
    monkeypatch.setenv("ROMA_LEDGER_DRIFT_THRESHOLD_USD", "0.5")
    assert drift_threshold_from_env() == 0.5


def test_threshold_garbage_falls_back(monkeypatch):
    for raw in ("", "   ", "abc", "-1", "nan", "inf"):
        monkeypatch.setenv("ROMA_LEDGER_DRIFT_THRESHOLD_USD", raw)
        assert drift_threshold_from_env() == DRIFT_THRESHOLD_DEFAULT


def test_reconcile_once_updates_gauges_and_last_success(monkeypatch):
    monkeypatch.setattr(PGBillingLedger, "reconciliation_summary", lambda self, t=None: SUMMARY)
    monkeypatch.setattr("main._set_backup_metric", lambda: None)
    monkeypatch.setattr("main._set_dr_drill_metrics", lambda: None)
    roma_ledger_reconcile_last_success_timestamp_seconds.set(0.0)

    summary = main._reconcile_once()

    assert summary["drifted"] == 2
    assert roma_ledger_reconciliation_diff_max._value.get() == 1.25
    assert roma_ledger_tenants_drifted._value.get() == 2
    assert roma_ledger_entry_count_total._value.get() == 309
    assert roma_spend_cap_exceeded_tenants._value.get() == 1
    assert roma_ledger_reconcile_last_success_timestamp_seconds._value.get() > 0


def test_reconcile_once_error_keeps_last_success(monkeypatch):
    roma_ledger_reconcile_last_success_timestamp_seconds.set(1234.0)

    def boom(self, t=None):
        raise RuntimeError("PG down")

    monkeypatch.setattr(PGBillingLedger, "reconciliation_summary", boom)
    monkeypatch.setattr("main._set_backup_metric", lambda: None)
    monkeypatch.setattr("main._set_dr_drill_metrics", lambda: None)

    try:
        main._reconcile_once()
    except RuntimeError:
        pass
    else:
        raise AssertionError("ошибка PG должна всплывать в loop")

    assert roma_ledger_reconcile_last_success_timestamp_seconds._value.get() == 1234.0


def test_script_exit_zero_without_drift(monkeypatch):
    monkeypatch.setattr(PGBillingLedger, "reconciliation_summary", lambda self, t=None: {**SUMMARY, "drifted": 0})
    assert _script_module().main() == 0


def test_script_exit_one_with_drift(monkeypatch):
    monkeypatch.setattr(PGBillingLedger, "reconciliation_summary", lambda self, t=None: SUMMARY)
    assert _script_module().main() == 1


def test_script_exit_one_when_pg_unavailable(monkeypatch):
    def boom(self, t=None):
        raise RuntimeError("PG down")

    monkeypatch.setattr(PGBillingLedger, "reconciliation_summary", boom)
    assert _script_module().main() == 1
