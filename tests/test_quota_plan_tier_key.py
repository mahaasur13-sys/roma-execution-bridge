"""G-QUOTA-SOURCE-REMNANTS-2 / Q-1: расхождение ключа plan/tier в find_tenant_by_key.

Дефект: PG-ветка `_find_tenant_by_key_pg` возвращала `{"tier": row[2]}` при SELECT
`id, name, plan`; sqlite-ветка возвращала `{"plan": row[2]}`. Потребители
(`main.py`, `routers/billing.py`) читают `.get("plan")`/`["plan"]` → на PG план
терялся (None → «free»), ломая spend-cap/квотную логику.

Фикс: PG-ветка выровнена на ключ `plan` (как sqlite). Тест — RED→GREEN по обеим
веткам (PG моком курсора, sqlite моком коннекта): ключ `plan` есть, `tier` нет.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db  # noqa: E402


def test_find_tenant_by_key_returns_plan_key_both_backends(monkeypatch):
    """Обе ветки (PG и sqlite) возвращают ключ 'plan', а не 'tier'."""

    # ── PG-ветка (мок курсора) ──
    class _FakePgCursor:
        def __init__(self, row):
            self._row = row

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return self._row

        def close(self):
            pass

    class _FakePgConn:
        def __init__(self, cur):
            self._cur = cur

        def cursor(self):
            return self._cur

        def commit(self):
            pass

        def rollback(self):
            pass

    pg_row = ("tenant-1", "Tenant One", "pro")  # id, name, plan
    monkeypatch.setattr(db, "_pg_conn", lambda: _FakePgConn(_FakePgCursor(pg_row)))
    monkeypatch.setattr(db, "_pg_return", lambda conn, **kw: None)
    monkeypatch.setattr(db, "_bump_tenant_lookup", lambda method: None)

    pg_result = db._find_tenant_by_key_pg("api-key-xxx")
    assert pg_result == {"tenant_id": "tenant-1", "name": "Tenant One", "plan": "pro"}
    assert "tier" not in pg_result

    # ── sqlite-ветка (мок коннекта) ──
    class _FakeSqliteConn:
        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            return self

        def fetchone(self):
            return self._row

    sq_row = ("tenant-1", "Tenant One", "pro", "api-key-xxx")  # id, name, plan, api_key
    monkeypatch.setattr(db, "_sqlite_conn", lambda: _FakeSqliteConn(sq_row))

    sq_result = db._find_tenant_by_key_sqlite("api-key-hash")
    assert sq_result == {
        "tenant_id": "tenant-1",
        "name": "Tenant One",
        "plan": "pro",
        "api_key": "api-key-xxx",
    }
    assert "tier" not in sq_result
