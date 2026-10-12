"""RaaS-01: the submit path must NOT write a submit-time cost estimate into
``usage_events``.

The estimate is already persisted via ``insert_decision_record(estimated_cost=…)``.
``usage_events`` is a journal of *real* metering (billed TRUE/FALSE/NULL); writing
an estimate there would pollute ``SUM(cost_usd)`` and look like a charge.
"""

import sys
import types

if "pydantic_settings" not in sys.modules:
    _fake = types.ModuleType("pydantic_settings")

    class _BaseSettings:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    _fake.BaseSettings = _BaseSettings
    sys.modules["pydantic_settings"] = _fake

import db_adapter
import decision_store
import main


def _active_tenant(tid):
    return {"tenant_id": tid, "subscription_status": "active", "plan": "free"}


def test_submit_does_not_write_estimate_into_usage_events(monkeypatch):
    usage_calls: list = []
    decision_calls: list = []

    # RaaS-04: this test is about usage_events (not the spend-cap guard), so
    # stub the guard to ALLOW and keep the submit flowing.
    monkeypatch.setattr(main, "_check_spend_cap", lambda *a, **k: (True, ""))

    # Spy: the only usage_events writer reachable from submit.
    monkeypatch.setattr(
        db_adapter,
        "insert_usage_event",
        lambda *a, **k: usage_calls.append((a, k)) or {"id": "x"},
    )
    monkeypatch.setattr(db_adapter, "insert_decision_request", lambda *a, **k: None)
    monkeypatch.setattr(db_adapter, "find_decision_by_idempotency", lambda *a, **k: None)
    monkeypatch.setattr(db_adapter, "get_tenant", _active_tenant)
    monkeypatch.setattr(db_adapter, "count_jobs_by_tenant", lambda tid: 0)
    monkeypatch.setattr(
        db_adapter,
        "insert_decision_record",
        lambda *a, **k: decision_calls.append((a, k)),
    )
    monkeypatch.setattr(db_adapter, "insert_job", lambda *a, **k: {"id": "job-1"})

    # Avoid the quota source (config/plans.json) and monitoring side-effects.
    monkeypatch.setattr(decision_store, "_get_plan_limit", lambda plan: 10)

    # Audit hooks: keep them no-ops.
    import audit.event_store as aes

    monkeypatch.setattr(aes, "on_decision_allowed", lambda *a, **k: None)
    monkeypatch.setattr(aes, "on_job_created", lambda *a, **k: None)

    res = decision_store.submit_job_through_gate("t1", "gpu_job", {"gpu": 1})

    # 1. insert_usage_event was NOT called.
    assert usage_calls == []

    # 2. Because that was the only usage_events writer on this path, usage_events
    #    remains empty for the submit (no estimate row, no billed row).
    #    (verified by the empty spy above; there is no other usage_events INSERT
    #    in submit_job_through_gate)

    # 3. estimated_cost is still persisted in the decision record and returned
    #    to the client (now from cost.gate.estimate_cost: payload has no
    #    gpu_required → 0.0001).
    assert decision_calls, "insert_decision_record should have been called"
    args = decision_calls[0][0]
    # insert_decision_record(decision_id, request_id, tenant_id, result, reason,
    #                        quota_remaining, estimated_cost, policy_name)
    assert args[6] == 0.0001
    assert res["estimated_cost"] == 0.0001
    assert res["job_id"] is not None
