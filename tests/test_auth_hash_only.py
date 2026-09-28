"""G-SEC4: auth по API-ключу — только hash-путь.

Контекст (INCIDENT ZO-SEC2 / ZO-SEC3): plaintext-ключи тенантов утекли в публичную
историю; в БД они были очищены и заменены хешами. Этот файл фиксирует код-контракт
после удаления plaintext-fallback и записи plaintext:

  * plaintext-ключ, у тенанта пустой ``api_key_hash`` → НЕ аутентифицируется
    (регрессия против возврата fallback по ``api_key``);
  * hash-ключ → аутентифицируется и возвращает ``{tenant_id, name, plan}``;
  * PG-ветка не выполняет ни одного SQL с ``WHERE api_key = ...`` (fallback убран);
  * создание тенанта (sqlite seed) пишет hash в ``api_key_hash`` и пустой ``api_key``;
  * поиск (sqlite) идёт по ``api_key_hash`` и отвергает неверный ключ.

Тесты — non-DB unit: PG-ветка проверяется моком курсора, sqlite — временным файлом.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as romadb  # noqa: E402
import db_adapter as dba  # noqa: E402


def _sha256(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


class _FakePgCursor:
    """Курсор-запись: запоминает SQL и параметры; строку отдаёт только при совпадении параметра.

    T1 (CodeRabbit #114): раньше курсор возвращал тенанта при ЛЮБОМ параметре, поэтому тест
    не ловил регрессию к plaintext-lookup. Теперь при ``keyed_on`` строка возвращается
    только если единственный параметр равен ожидаемому (обычно — хеш ключа).
    """

    def __init__(self, row, keyed_on=None):
        self._row = row
        self._keyed_on = keyed_on
        self._match = keyed_on is None
        self.executed: list[str] = []
        self.params: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        recorded = tuple(params) if params else ()
        self.params.append(recorded)
        if self._keyed_on is not None:
            self._match = recorded == (self._keyed_on,)

    def fetchone(self):
        if self._keyed_on is None:
            return self._row
        return self._row if self._match else None

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


def _install_pg(monkeypatch, row, keyed_on=None):
    cur = _FakePgCursor(row, keyed_on=keyed_on)
    monkeypatch.setattr(dba, "_pg_conn", lambda: _FakePgConn(cur))
    monkeypatch.setattr(dba, "_pg_return", lambda conn, **kw: None)
    monkeypatch.setattr(dba, "_bump_tenant_lookup", lambda method: None)
    return cur


def test_pg_plaintext_key_with_empty_hash_is_not_authenticated(monkeypatch):
    """Ключ, у которого в БД пустой hash (старый plaintext), больше не проходит."""
    cur = _install_pg(monkeypatch, None)  # hash-запрос не нашёл строку

    assert dba._find_tenant_by_key_pg("roma-legacy-plaintext-key") is None
    # fallback не должен выполняться: только один SELECT за вызов.
    assert len(cur.executed) == 1


def test_pg_lookup_never_issues_plaintext_sql(monkeypatch):
    """Ни один SQL lookup не содержит сравнения по plaintext-колонке api_key."""
    cur = _install_pg(monkeypatch, None)

    dba._find_tenant_by_key_pg("some-key")

    joined = " ".join(cur.executed).lower()
    assert "api_key_hash" in joined
    assert "where api_key =" not in joined.replace("api_key_hash", "hash")
    assert not any(
        "where api_key =" in sql.lower() and "api_key_hash" not in sql.lower()
        for sql in cur.executed
    )


def test_pg_hash_key_is_authenticated(monkeypatch):
    """Валидный hash-ключ возвращает tenant_id/name/plan без plaintext-полей.

    T1: курсор отдаёт строку ТОЛЬКО если в SQL ушёл хеш ключа, а не сам ключ.
    """
    cur = _install_pg(
        monkeypatch, ("tenant-1", "Tenant One", "pro"), keyed_on=_sha256("valid-key")
    )

    result = dba._find_tenant_by_key_pg("valid-key")

    assert result == {"tenant_id": "tenant-1", "name": "Tenant One", "plan": "pro"}
    assert "api_key" not in result
    assert "tier" not in result

    # В параметры обязан уйти sha256(ключа), и нигде не должно быть plaintext.
    assert cur.params == [(_sha256("valid-key"),)]
    flat = [str(v) for params in cur.params for v in params]
    assert "valid-key" not in flat


def test_pg_wrong_key_is_not_authenticated(monkeypatch):
    """T1 negative: при неверном ключе (другой хеш в параметре) tenant не находится."""
    _install_pg(
        monkeypatch, ("tenant-1", "Tenant One", "pro"), keyed_on=_sha256("valid-key")
    )

    assert dba._find_tenant_by_key_pg("wrong-key") is None


def test_sqlite_seed_stores_hash_not_plaintext(tmp_path, monkeypatch):
    """Создание тенанта пишет hash, а plaintext api_key остаётся пустым."""
    monkeypatch.setattr(romadb, "DB_PATH", tmp_path / "roma.db")
    romadb.init_db()

    romadb.seed_tenants({"roma-secret-key-xyz": {"tenant_id": "tenant-x", "name": "X"}})

    conn = sqlite3.connect(str(romadb.DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT api_key, api_key_hash FROM tenants WHERE id = 'tenant-x'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["api_key"] == ""
    assert row["api_key_hash"] == _sha256("roma-secret-key-xyz")


def test_sqlite_find_uses_hash_and_rejects_wrong_key(tmp_path, monkeypatch):
    """sqlite-поиск идёт по api_key_hash: верный ключ → найден, неверный → None."""
    monkeypatch.setattr(romadb, "DB_PATH", tmp_path / "roma.db")
    romadb.init_db()

    conn = sqlite3.connect(str(romadb.DB_PATH))
    conn.execute(
        "INSERT INTO tenants (id, api_key, api_key_hash, name, plan)"
        " VALUES (?, '', ?, ?, ?)",
        ("tenant-y", _sha256("good-key"), "Y", "free"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        dba, "_sqlite_conn", lambda: sqlite3.connect(str(romadb.DB_PATH))
    )

    found = dba._find_tenant_by_key_sqlite("good-key")
    assert found == {"tenant_id": "tenant-y", "name": "Y", "plan": "free"}

    assert dba._find_tenant_by_key_sqlite("wrong-key") is None
    assert dba._find_tenant_by_key_sqlite("") is None


def test_find_tenant_by_key_dispatches_hash_only(monkeypatch):
    """Публичная точка входа не возвращает plaintext-поля ни на одной ветке."""
    monkeypatch.setattr(dba, "_pg_enabled", lambda: True)
    _install_pg(monkeypatch, ("tenant-z", "Z", "start"))

    result = dba.find_tenant_by_key("any-key")

    assert result == {"tenant_id": "tenant-z", "name": "Z", "plan": "start"}
    assert "api_key" not in result
