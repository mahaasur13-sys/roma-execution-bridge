#!/usr/bin/env python3
"""Семантика `_LEDGER_SUMMARY_SQL`: FULL OUTER JOIN ledger↔usage и порог.

Исполняется только при живом PostgreSQL (`PG_DSN`): схема поднимается во временных
таблицах одного соединения, прод не затрагивается. Без PG — skip (env-skip).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, ".")

from billing.pg_ledger import DRIFT_THRESHOLD_DEFAULT, _LEDGER_SUMMARY_SQL

pytestmark = pytest.mark.pg

_DDL = (
    "CREATE TEMP TABLE tenants (id text PRIMARY KEY, plan text) ON COMMIT PRESERVE ROWS",
    """CREATE TEMP TABLE ledger_entries (
           tenant_id text, entry_type text, amount double precision
       ) ON COMMIT PRESERVE ROWS""",
    """CREATE TEMP TABLE usage_events (
           tenant_id text, cost_usd double precision
       ) ON COMMIT PRESERVE ROWS""",
)


def _run(ledger_rows, usage_rows, tenants=(), threshold=DRIFT_THRESHOLD_DEFAULT):
    import psycopg2

    caps = json.dumps({"free": 0.50, "start": 5.0, "pro": 50.0, "enterprise": -1.0})
    conn = psycopg2.connect(os.environ["PG_DSN"])
    try:
        cur = conn.cursor()
        for ddl in _DDL:
            cur.execute(ddl)
        for tenant_id, entry_type, amount in ledger_rows:
            cur.execute("INSERT INTO ledger_entries VALUES (%s, %s, %s)", (tenant_id, entry_type, amount))
        for tenant_id, cost in usage_rows:
            cur.execute("INSERT INTO usage_events VALUES (%s, %s)", (tenant_id, cost))
        for tenant_id, plan in tenants:
            cur.execute("INSERT INTO tenants VALUES (%s, %s)", (tenant_id, plan))
        cur.execute(_LEDGER_SUMMARY_SQL, (threshold, threshold, caps, caps))
        diff_max, drifted, entries, top, exceeded = cur.fetchone()
        return {
            "diff_max": float(diff_max),
            "drifted": int(drifted),
            "entries_total": int(entries),
            "drifted_top": list(top or []),
            "spend_cap_exceeded": int(exceeded),
        }
    finally:
        conn.rollback()
        conn.close()


def test_no_data_is_all_zeros():
    s = _run([], [])
    assert s == {"diff_max": 0.0, "drifted": 0, "entries_total": 0, "drifted_top": [], "spend_cap_exceeded": 0}


def test_exact_match_is_no_drift():
    s = _run([("t-a", "DEBIT", 0.25), ("t-a", "DEBIT", 0.25)], [("t-a", 0.5)])
    assert s["diff_max"] == pytest.approx(0.0)
    assert s["drifted"] == 0
    assert s["entries_total"] == 2


def test_ledger_only_tenant_counts_as_drift():
    s = _run([("t-ledger", "DEBIT", 1.5)], [])
    assert s["diff_max"] == pytest.approx(1.5)
    assert s["drifted"] == 1
    assert [r["tenant_id"] for r in s["drifted_top"]] == ["t-ledger"]


def test_usage_only_tenant_counts_as_drift():
    s = _run([], [("t-usage", 2.0)])
    assert s["diff_max"] == pytest.approx(2.0)
    assert s["drifted"] == 1
    assert s["drifted_top"][0]["tenant_id"] == "t-usage"


def test_drift_below_threshold_does_not_count():
    s = _run([("t-a", "DEBIT", 0.30)], [("t-a", 0.29)])
    assert s["diff_max"] == pytest.approx(0.01, abs=1e-9)
    assert s["drifted"] == 0
    assert s["drifted_top"] == []


def test_drift_above_threshold_counts_and_sorts_desc():
    s = _run([("t-a", "DEBIT", 1.0), ("t-b", "DEBIT", 0.2)], [], threshold=0.05)
    assert s["drifted"] == 2
    assert [r["tenant_id"] for r in s["drifted_top"]] == ["t-a", "t-b"]


def test_spend_cap_exceeded_counts_tenants_over_their_plan_cap():
    s = _run(
        [("t-free", "DEBIT", 0.60), ("t-pro", "DEBIT", 1.0)],
        [],
        tenants=[("t-free", "free"), ("t-pro", "pro")],
    )
    assert s["spend_cap_exceeded"] == 1
