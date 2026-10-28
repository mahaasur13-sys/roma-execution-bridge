"""ROMA_DB_INIT_DDL=0: DDL на старте не выполняется, схема только проверяется.

Проверяем поведение db_pg_sync.init_db на фейковом соединении — без PG.
"""

import pytest

import db_pg_sync


class _FakeCursor:
    def __init__(self, existing):
        self.existing = set(existing)
        self.queries = []
        self._row = None

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if sql.startswith("SELECT to_regclass"):
            name = params[0]
            self._row = (name if name in self.existing else None,)
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, existing=None):
        self.cur = _FakeCursor(db_pg_sync.EXPECTED_TABLES if existing is None else existing)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _ddl_statements(conn):
    return [sql for sql, _ in conn.cur.queries if "CREATE " in sql.upper()]


def test_ddl_runs_when_flag_unset(monkeypatch):
    monkeypatch.delenv("ROMA_DB_INIT_DDL", raising=False)
    conn = _FakeConn()

    db_pg_sync.init_db(conn)

    assert _ddl_statements(conn), "поумолчанию init_db обязан выполнять DDL"
    assert not [q for q, _ in conn.cur.queries if q.startswith("SELECT to_regclass")]
    assert conn.commits >= 1


def test_ddl_runs_when_flag_is_one(monkeypatch):
    monkeypatch.setenv("ROMA_DB_INIT_DDL", "1")
    conn = _FakeConn()

    db_pg_sync.init_db(conn)

    assert _ddl_statements(conn)


def test_ddl_skipped_and_schema_checked_when_disabled(monkeypatch):
    monkeypatch.setenv("ROMA_DB_INIT_DDL", "0")
    conn = _FakeConn()

    assert db_pg_sync.init_db(conn) is None

    assert not _ddl_statements(conn), "при ROMA_DB_INIT_DDL=0 DDL запрещён"
    checked = [params[0] for sql, params in conn.cur.queries if sql.startswith("SELECT to_regclass")]
    assert checked == list(db_pg_sync.EXPECTED_TABLES)


def test_missing_table_fails_closed(monkeypatch):
    monkeypatch.setenv("ROMA_DB_INIT_DDL", "0")
    present = list(db_pg_sync.EXPECTED_TABLES)
    present.remove("users")
    conn = _FakeConn(present)

    with pytest.raises(RuntimeError) as exc:
        db_pg_sync.init_db(conn)

    assert "users" in str(exc.value)
    assert "tenants" not in str(exc.value)
    assert not _ddl_statements(conn)


def test_error_lists_every_missing_table(monkeypatch):
    monkeypatch.setenv("ROMA_DB_INIT_DDL", "0")
    conn = _FakeConn([t for t in db_pg_sync.EXPECTED_TABLES if t not in ("leads", "feedback")])

    with pytest.raises(RuntimeError) as exc:
        db_pg_sync.init_db(conn)

    message = str(exc.value)
    assert "leads" in message and "feedback" in message
