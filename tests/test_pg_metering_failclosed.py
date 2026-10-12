"""RaaS-01: PGMeteringEngine.record must fail closed, not silently fall back.

Before RaaS-01 the durable INSERT failure (e.g. the missing ``usage_events.billed``
column) was swallowed with ``except PGUnavailableError: pass`` and ``record``
returned a UsageEvent backed only by in-memory state — a false success for paid
work. After the fix, a failed durable write raises PGUnavailableError so the
caller (execution_worker) can fail closed.
"""

import pytest

from billing.pg_metering import PGMeteringEngine
from billing.pg_connection import PGUnavailableError


def test_record_fails_closed_when_pg_write_fails(monkeypatch):
    engine = PGMeteringEngine()

    def _boom(*args, **kwargs):
        raise PGUnavailableError("simulated PG down")

    monkeypatch.setattr(engine, "_pg_execute", _boom)

    with pytest.raises(PGUnavailableError):
        engine.record("gpu_usage", tenant="t1", gpu_seconds=10.0, job_id="j1")


def test_record_attempts_both_durable_writes_and_returns_event(monkeypatch):
    engine = PGMeteringEngine()
    calls: list[str] = []

    def _ok(operation, query, params=None, fetch=False):
        calls.append(operation)
        return None

    monkeypatch.setattr(engine, "_pg_execute", _ok)

    ev = engine.record(
        "gpu_usage", tenant="t1", gpu_seconds=10.0, job_id="j1", billed=True
    )
    assert ev.tenant_id == "t1"
    assert ev.value == 10.0
    # usage_insert must be attempted first, then the totals upsert.
    assert calls == ["usage_insert", "usage_totals_upsert"]
