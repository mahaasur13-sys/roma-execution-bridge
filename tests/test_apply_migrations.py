"""ops/migrations-runner: DDL runs as roma_owner, the ledger gates what runs, no DSN leaks.

PG-free: the runner is driven through a fake connection, so the ordering
(`SET LOCAL ROLE` before the body), the ledger skip, the rollback contract and the
ownership gate are pinned without a database. The real PG path is exercised by
the existing migrations suite and by the operational `--dry-run`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DSN = "postgresql://admin:s3cr3t-value@127.0.0.1:5432/roma"


def _load_runner():
    spec = importlib.util.spec_from_file_location("apply_migrations", str(SCRIPTS / "apply_migrations.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mig(directory: Path, name: str, body: str = "SELECT 1;") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def execute(self, sql, params=None) -> None:
        text = " ".join(str(sql).split())
        self._conn.statements.append((text, params))
        if self._conn.fail_on and self._conn.fail_on in text:
            raise RuntimeError("boom")

    def fetchone(self):
        return self._conn.fetchone_queue.pop(0) if self._conn.fetchone_queue else None

    def fetchall(self):
        return self._conn.fetchall_queue.pop(0) if self._conn.fetchall_queue else []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _Conn:
    def __init__(self, fetchone=(), fetchall=(), fail_on: str | None = None) -> None:
        self.statements: list[tuple[str, object]] = []
        self.fetchone_queue = list(fetchone)
        self.fetchall_queue = list(fetchall)
        self.fail_on = fail_on
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = True

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


def _applied_all(runner) -> list[tuple[str]]:
    return [(path.name,) for path in runner.run_migrations._migration_files()]


def test_pending_skips_applied_keeps_order(tmp_path) -> None:
    runner = _load_runner()
    files = [_mig(tmp_path, n) for n in ("001_a.sql", "002_b.sql", "003_c.sql")]

    assert [p.name for p in runner.pending(files, {"002_b.sql"})] == ["001_a.sql", "003_c.sql"]


def test_set_local_role_precedes_body_and_ledger(tmp_path) -> None:
    runner = _load_runner()
    conn = _Conn()
    path = _mig(tmp_path, "001_a.sql", "CREATE TABLE t (id int);")

    assert runner.apply_pending(conn, [path]) == 1

    sqls = [sql for sql, _ in conn.statements]
    assert sqls[0] == "SET LOCAL ROLE roma_owner"
    assert sqls[1] == "CREATE TABLE t (id int);"
    assert sqls[2].startswith("INSERT INTO schema_migrations")
    assert conn.commits == 1 and conn.rollbacks == 0


def test_ledger_is_created_and_pinned_to_owner_before_first_migration() -> None:
    """On a fresh DB the ledger is bootstrapped by the admin and owned by roma_owner."""
    runner = _load_runner()
    conn = _Conn()

    runner.ensure_ledger(conn)

    sqls = [sql for sql, _ in conn.statements]
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in sqls[0]
    assert sqls[1] == "ALTER TABLE schema_migrations OWNER TO roma_owner"
    assert conn.commits == 0 and conn.rollbacks == 0


def test_failed_migration_rolls_back(tmp_path) -> None:
    runner = _load_runner()
    conn = _Conn(fail_on="CREATE TABLE")
    path = _mig(tmp_path, "001_a.sql", "CREATE TABLE t (id int);")

    with pytest.raises(RuntimeError):
        runner.apply_pending(conn, [path])

    assert conn.rollbacks == 1 and conn.commits == 0


def test_main_returns_1_when_migration_fails(tmp_path, monkeypatch, capsys) -> None:
    runner = _load_runner()
    conn = _Conn(fetchone=[(1,), ("schema_migrations",)], fetchall=[[]], fail_on="SET LOCAL ROLE")
    monkeypatch.setattr(runner, "_connect", lambda dsn: conn)
    monkeypatch.setenv("ROMA_ADMIN_PG_DSN", DSN)

    assert runner.main([]) == 1
    assert "migration failed" in capsys.readouterr().err


def test_dry_run_writes_nothing_and_lists_pending(monkeypatch, capsys) -> None:
    runner = _load_runner()
    total = len(runner.run_migrations._migration_files())
    conn = _Conn(fetchone=[(1,), ("schema_migrations",)], fetchall=[[]])
    monkeypatch.setattr(runner, "_connect", lambda dsn: conn)
    monkeypatch.setenv("ROMA_ADMIN_PG_DSN", DSN)

    assert runner.main(["--dry-run"]) == 0

    out = capsys.readouterr()
    assert f"dry-run: {total} pending of {total}" in out.out
    for sql, _ in conn.statements:
        assert not sql.upper().startswith(("SET ", "INSERT", "CREATE", "DROP", "ALTER")), sql
    assert "s3cr3t-value" not in out.out + out.err


def test_foreign_owner_fails(monkeypatch, capsys) -> None:
    runner = _load_runner()
    conn = _Conn(
        fetchone=[(1,), ("schema_migrations",)],
        fetchall=[_applied_all(runner), [("legacy_table",), ("legacy_fn",)]],
    )
    monkeypatch.setattr(runner, "_connect", lambda dsn: conn)
    monkeypatch.setenv("ROMA_ADMIN_PG_DSN", DSN)

    assert runner.main([]) == 1

    err = capsys.readouterr().err
    assert "legacy_table" in err and "legacy_fn" in err


def test_missing_owner_role_refuses(monkeypatch, capsys) -> None:
    runner = _load_runner()
    conn = _Conn(fetchone=[None])
    monkeypatch.setattr(runner, "_connect", lambda dsn: conn)
    monkeypatch.setenv("ROMA_ADMIN_PG_DSN", DSN)

    assert runner.main([]) == 2
    assert "roma_owner is missing" in capsys.readouterr().err
    assert len(conn.statements) == 1, conn.statements


def test_missing_dsn_refuses(tmp_path, monkeypatch) -> None:
    runner = _load_runner()
    monkeypatch.delenv("ROMA_ADMIN_PG_DSN", raising=False)

    assert runner.main([]) == 2
    assert runner.main(["--dsn-file", str(tmp_path / "absent.txt")]) == 2


def test_dsn_file_accepts_bare_and_key_value(tmp_path, monkeypatch) -> None:
    runner = _load_runner()
    monkeypatch.delenv("ROMA_ADMIN_PG_DSN", raising=False)

    bare = _mig(tmp_path, "bare.txt", f"# admin\n{DSN}\n")
    keyed = _mig(tmp_path, "keyed.txt", f"ROMA_ADMIN_PG_DSN={DSN}\n")

    assert runner.admin_dsn(str(bare)) == DSN
    assert runner.admin_dsn(str(keyed)) == DSN


def test_env_dsn_wins_over_file(tmp_path, monkeypatch) -> None:
    runner = _load_runner()
    keyed = _mig(tmp_path, "keyed.txt", "ROMA_ADMIN_PG_DSN=postgresql://from:file@127.0.0.1:5432/roma\n")
    monkeypatch.setenv("ROMA_ADMIN_PG_DSN", DSN)

    assert runner.admin_dsn(str(keyed)) == DSN
