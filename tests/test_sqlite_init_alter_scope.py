"""G-SEC4/M2 (CodeRabbit #114): scope of the sqlite ALTER for ``tenants.api_key_hash``.

Defect class: the ADD COLUMN was wrapped in a blanket ``except OperationalError``,
so *any* sqlite failure (locked DB, corrupt file, missing table) was silently
swallowed — the schema could stay without ``api_key_hash`` while startup looked
successful. The fix probes ``PRAGMA table_info(tenants)`` and only runs the DDL
when the column is truly absent, so unrelated OperationalError propagates.
"""

from __future__ import annotations

import sqlite3

import pytest

import db


def test_init_db_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "roma.db")
    db.init_db()
    db.init_db()
    with sqlite3.connect(str(tmp_path / "roma.db")) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(tenants)").fetchall()}
    assert "api_key_hash" in cols
    assert "api_key" in cols


def test_unrelated_operational_error_propagates(monkeypatch):
    class _FakeConn:
        def executescript(self, _sql):
            return None

        def execute(self, sql, *_a):
            if sql.lstrip().upper().startswith("PRAGMA TABLE_INFO"):
                raise sqlite3.OperationalError("database is locked")
            raise AssertionError(f"unexpected SQL: {sql}")

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(db, "_conn", lambda: _FakeConn())
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        db.init_db()
