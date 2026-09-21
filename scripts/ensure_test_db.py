#!/usr/bin/env python3
"""Идемпотентно создать тестовую БД `roma_test` и применить к ней миграции.

Зачем: A1 требует, чтобы тесты исполнялись против тестовой БД, а не против
боевой. Скрипт можно запускать повторно — повторный запуск ничего не ломает.

Порядок:
  1. боевой DSN ищется в PG_DSN процесса, затем в `.env` (как `env_loader`);
  2. имя БД заменяется на `roma_test` (креденшлы/хост/порт сохраняются);
  3. `CREATE DATABASE roma_test` — только если её ещё нет;
  4. `db_adapter.init_db()` и `scripts/run_migrations.py` применяются к ней.

Использует `tests/_isolation.py` — ту же логику разбора DSN, что и guard.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT))

import _isolation as iso  # noqa: E402

TEST_DB_NAME = iso.TEST_DB_NAME


def _source_dsn() -> str | None:
    return (
        iso._env_or_none("ROMA_TEST_PG_DSN")
        or iso._env_or_none("TEST_PG_DSN")
        or iso._env_or_none("PG_DSN")
        or iso.prod_dsn()
    )


def _admin_conn_kwargs(dsn: str) -> dict:
    parsed = iso.parse_dsn(dsn) or {}
    kwargs = {
        "host": parsed.get("host") or "localhost",
        "port": parsed.get("port") or 5432,
        "dbname": "postgres",
        "connect_timeout": 5,
    }
    raw = dsn.strip()
    if parsed.get("form") == "url":
        import urllib.parse

        url = urllib.parse.urlparse(iso._URL_DRIVER_RE.sub(r"\1://", raw))
        if url.username:
            kwargs["user"] = urllib.parse.unquote(url.username)
        if url.password:
            kwargs["password"] = urllib.parse.unquote(url.password)
    else:
        values = {}
        for match in iso._LIBPQ_KV_RE.finditer(raw):
            values[match.group(1).lower()] = match.group(2)
        for key in ("user", "password"):
            if values.get(key):
                kwargs[key] = values[key]
    return kwargs


def ensure_database(dsn: str) -> str:
    """Создать `roma_test`, если её нет. Возвращает статус: created|exists."""
    import psycopg2
    from psycopg2 import sql

    kwargs = _admin_conn_kwargs(dsn)
    with psycopg2.connect(**kwargs) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,))
            if cur.fetchone():
                return "exists"
            cur.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB_NAME))
            )
            return "created"


def main() -> int:
    source = _source_dsn()
    if not source:
        print("ensure_test_db: не найден исходный DSN (PG_DSN пуст и .env отсутствует)")
        return 2

    test_dsn = iso.derive_test_dsn(source)
    if not test_dsn:
        print("ensure_test_db: не удалось вывести DSN тестовой БД")
        return 2

    print(f"source : {iso.describe(source)}")
    print(f"target : {iso.describe(test_dsn)}")
    print(f"database: {ensure_database(test_dsn)}")

    env = dict(os.environ)
    env["PG_DSN"] = test_dsn
    env.pop("ROMA_TEST_PG_DSN", None)

    bootstrap = subprocess.run(
        [sys.executable, "-c", "import db_adapter; db_adapter.init_db()"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    print(f"db_adapter.init_db: exit={bootstrap.returncode}")
    if bootstrap.returncode != 0:
        print(bootstrap.stderr.strip()[-500:])
        return bootstrap.returncode

    migrations = subprocess.run(
        [sys.executable, "scripts/run_migrations.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    print("migrations (scripts/run_migrations.py):")
    print("\n".join(line for line in migrations.stdout.splitlines() if line.strip()))
    if migrations.returncode != 0:
        print(migrations.stderr.strip()[-500:])
        return migrations.returncode

    print("ensure_test_db: OK (идемпотентно)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
