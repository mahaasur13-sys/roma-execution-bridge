"""F2b: db_adapter.get_user_by_api_key не ссылается на удалённые имена db_pg_sync.

Контекст: в PR #175 удалён мёртвый дубль ``db_pg_sync.get_user_by_api_key``
(живая реализация — ``find_user_by_api_key``). Диспетчер в ``db_adapter``
продолжал импортировать старое имя, и PG-ветка падала бы ImportError на
первом же аутентифицированном запросе.

Фиксируем:

  * PG-ветка делегирует в ``db_pg_sync.find_user_by_api_key`` и возвращает его результат;
  * sqlite-ветка по-прежнему идёт в ``db.get_user_by_api_key``;
  * source-pin: каждое ``from db_pg_sync import X`` в ``db_adapter.py`` существует в модуле.

Тесты — non-DB unit: PG-соединение и реализация мокаются, sqlite не поднимается.
"""

from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as romadb  # noqa: E402
import db_adapter as dba  # noqa: E402
import db_pg_sync  # noqa: E402


def test_pg_branch_delegates_to_find_user_by_api_key(monkeypatch):
    """PG-ветка вызывает find_user_by_api_key(conn, key) и отдаёт её результат."""
    sentinel_conn = object()
    calls = []
    expected = {"id": "u-1", "email": "a@b.c"}

    def fake_find(conn, api_key):
        calls.append((conn, api_key))
        return expected

    monkeypatch.setattr(dba, "_pg_enabled", lambda: True)
    monkeypatch.setattr(dba, "_pg_conn", lambda: sentinel_conn)
    monkeypatch.setattr(dba, "_pg_return", lambda conn, *a, **kw: None)
    monkeypatch.setattr(db_pg_sync, "find_user_by_api_key", fake_find)

    assert dba.get_user_by_api_key("raw-key-1") == expected
    assert calls == [(sentinel_conn, "raw-key-1")]


def test_sqlite_branch_falls_back_to_db_module(monkeypatch):
    """Без PG диспетчер идёт в db.get_user_by_api_key с сырым ключом."""
    expected = {"id": "u-2"}
    seen = []

    def fake_db_lookup(api_key):
        seen.append(api_key)
        return expected

    monkeypatch.setattr(dba, "_pg_enabled", lambda: False)
    monkeypatch.setattr(romadb, "get_user_by_api_key", fake_db_lookup)

    assert dba.get_user_by_api_key("raw-key-2") == expected
    assert seen == ["raw-key-2"]


def test_source_pin_db_pg_sync_imports_exist():
    """Ни один `from db_pg_sync import X` в db_adapter.py не ссылается на несуществующее имя."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db_adapter.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "db_pg_sync"
        for alias in node.names
    ]

    assert imported, "в db_adapter.py не найдено ни одного импорта из db_pg_sync"
    missing = [name for name in imported if not hasattr(db_pg_sync, name)]
    assert missing == [], f"db_adapter.py импортирует отсутствующие в db_pg_sync имена: {missing}"
