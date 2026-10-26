#!/usr/bin/env python3
"""reconciliation_summary — агрегатный разбор ответа PG, без БД (executor подменён)."""

import json
import sys

sys.path.insert(0, ".")

from billing.pg_ledger import DRIFT_THRESHOLD_DEFAULT, _LEDGER_SUMMARY_SQL, PGBillingLedger


def _ledger_returning(row):
    """Ledger, у которого _pg_execute отдаёт одну строку агрегатов и пишет вызовы."""
    ledger = PGBillingLedger()
    calls = []

    def fake_execute(operation, query, params=None, fetch=False):
        calls.append((operation, params))
        return [row]

    ledger._pg_execute = fake_execute
    return ledger, calls


def test_no_rows_is_all_zeros():
    ledger = PGBillingLedger()
    ledger._pg_execute = lambda *a, **k: []

    s = ledger.reconciliation_summary(0.05)

    assert (s["diff_max"], s["drifted"], s["entries_total"], s["spend_cap_exceeded"]) == (0.0, 0, 0, 0)
    assert s["drifted_top"] == []
    assert s["threshold_usd"] == 0.05


def test_exact_match_no_drift_sends_one_query_with_threshold_and_caps():
    ledger, calls = _ledger_returning((0.0, 0, 309, [], 0))

    s = ledger.reconciliation_summary(0.05)

    assert (s["diff_max"], s["drifted"], s["entries_total"]) == (0.0, 0, 309)
    assert len(calls) == 1
    operation, params = calls[0]
    assert operation == "ledger_reconcile_summary"
    assert params[0] == params[1] == 0.05
    caps = json.loads(params[2])
    assert params[3] == params[2]
    assert caps["free"] == 0.50 and caps["enterprise"] == -1.0


def test_drift_above_threshold_surfaces_top_and_spend_cap():
    top = [{"tenant_id": "t-a", "diff": 1.25}, {"tenant_id": "t-b", "diff": 0.5}]
    ledger, _ = _ledger_returning((1.25, 2, 40, top, 3))

    s = ledger.reconciliation_summary(0.05)

    assert s["diff_max"] == 1.25
    assert s["drifted"] == 2
    assert [r["tenant_id"] for r in s["drifted_top"]] == ["t-a", "t-b"]
    assert s["spend_cap_exceeded"] == 3


def test_none_threshold_falls_back_to_default():
    ledger, calls = _ledger_returning((0.0, 0, 0, None, 0))

    s = ledger.reconciliation_summary()

    assert s["threshold_usd"] == DRIFT_THRESHOLD_DEFAULT
    assert calls[0][1][0] == DRIFT_THRESHOLD_DEFAULT


def test_sql_has_no_hardcoded_tenant():
    """Регресс-гард: сверка агрегатная, а не по одному литеральному тенанту."""
    assert "t-test-paper" not in _LEDGER_SUMMARY_SQL
    assert "FULL OUTER JOIN" in _LEDGER_SUMMARY_SQL
    assert "LIMIT 20" in _LEDGER_SUMMARY_SQL
