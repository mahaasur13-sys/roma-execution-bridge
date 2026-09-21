"""P1/T2 — readiness self-heal.

Pre-fix behaviour: `PGConnectionManager.health()` short-circuited with
`connected=False` as soon as `_pg_available` was False and never re-probed,
so `/ready` stayed 503 forever after a PG blip (until an unrelated query
succeeded).  Post-fix: `health()` re-probes via `_ensure_pool()`.
"""
from __future__ import annotations

import billing.pg_connection as pgc


class _Cur:
    def execute(self, *a, **kw):
        return None

    def fetchone(self):
        return ("PostgreSQL 15.0 on x86_64",)

    def close(self):
        return None


class _Conn:
    def cursor(self):
        return _Cur()


class _Ctx:
    def __enter__(self):
        return _Conn()

    def __exit__(self, *a):
        return False


def test_health_reprobes_when_unavailable(monkeypatch):
    monkeypatch.setattr(pgc, "PG_DSN", "postgresql://u:p@localhost:5432/db")
    mgr = pgc.PGConnectionManager()
    mgr._pg_available = False

    calls: list[int] = []

    def fake_ensure_pool():
        calls.append(1)
        mgr._pg_available = True
        return True

    monkeypatch.setattr(mgr, "_ensure_pool", fake_ensure_pool)
    monkeypatch.setattr(mgr, "get_connection", lambda operation="query": _Ctx())

    info = mgr.health()

    assert calls == [1], "health() must re-probe when _pg_available is False"
    assert info["connected"] is True
    assert info["status"] == "healthy"


def test_health_stays_unavailable_when_probe_fails(monkeypatch):
    monkeypatch.setattr(pgc, "PG_DSN", "postgresql://u:p@localhost:5432/db")
    mgr = pgc.PGConnectionManager()
    mgr._pg_available = False
    monkeypatch.setattr(mgr, "_ensure_pool", lambda: False)

    info = mgr.health()

    assert info["connected"] is False
    assert info["status"] == "unavailable"
