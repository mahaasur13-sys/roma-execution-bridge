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

    class FakeMgr:
        enabled = True

        def get_connection(self, operation):
            raise PGUnavailableError("simulated PG down")

    monkeypatch.setattr(engine, "_pg", FakeMgr())

    with pytest.raises(PGUnavailableError):
        engine.record("gpu_usage", tenant="t1", gpu_seconds=10.0, job_id="j1")


def test_record_returns_inserted_and_orders_totals_after(monkeypatch):
    engine = PGMeteringEngine()
    executed: list[str] = []

    class FakeCursor:
        def __init__(self):
            self._sql = ""

        def execute(self, sql, params=None):
            self._sql = sql
            executed.append("usage" if "usage_events" in sql else "totals")

        def fetchall(self):
            return [(1,)] if "usage_events" in self._sql else []

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

    class FakeMgr:
        enabled = True

        def get_connection(self, operation):
            class Ctx:
                def __enter__(self):
                    return FakeConn()

                def __exit__(self, *a):
                    return False

            return Ctx()

    monkeypatch.setattr(engine, "_pg", FakeMgr())

    ev, inserted = engine.record("gpu_usage", tenant="t1", gpu_seconds=10.0, job_id="j1", billed=True)
    assert ev.tenant_id == "t1"
    assert ev.value == 10.0
    assert inserted is True
    # usage INSERT — первое действие, totals — строго после него (одна транзакция).
    assert executed == ["usage", "totals"]
