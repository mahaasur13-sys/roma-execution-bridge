"""A3: migration runner — deterministic order + unique numeric prefixes."""

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_migrations", str(REPO_ROOT / "scripts" / "run_migrations.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run_migrations = _load_runner()


def test_migration_numeric_prefixes_are_unique():
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert names, "no migration files found"

    prefixes = [re.match(r"^(\d+)", n).group(1) for n in names]
    # No duplicate numeric prefix (the two-005 collision is resolved).
    assert len(prefixes) == len(set(prefixes)), f"duplicate prefix in: {names}"
    # Lexicographic order == application order (no gaps that break sorting).
    assert prefixes == sorted(prefixes), f"not ordered: {names}"


def test_runner_is_noop_without_pg(monkeypatch):
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert run_migrations.main() == 0


def test_strip_txn_wrappers():
    sql = "BEGIN;\nALTER TABLE x ADD COLUMN y INT;\nCOMMIT;\n"
    assert run_migrations._strip_txn_wrappers(sql) == "ALTER TABLE x ADD COLUMN y INT;"
