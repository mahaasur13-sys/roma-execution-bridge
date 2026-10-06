"""Tests for the unified .env loader (C6)."""

from __future__ import annotations

import os

import pytest

import env_loader


@pytest.fixture(autouse=True)
def _reset_loaded(monkeypatch):
    """Reset the idempotency flag so each test can exercise load_env()."""
    monkeypatch.setattr(env_loader, "_loaded", False)


def test_env_wins_over_file(monkeypatch, tmp_path):
    """A pre-set os.environ value must not be overridden by .env."""
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_C6_KEY=from_file\n")
    monkeypatch.setenv("TEST_C6_KEY", "from_env")
    monkeypatch.setattr(env_loader, "_find_env_file", lambda: env_file)

    result = env_loader.load_env()

    assert result == env_file
    assert os.environ["TEST_C6_KEY"] == "from_env"


def test_missing_env_file_is_ok(monkeypatch):
    """A missing .env must not raise."""
    monkeypatch.setattr(env_loader, "_find_env_file", lambda: None)

    result = env_loader.load_env()

    assert result is None


def test_file_value_loaded_when_env_unset(monkeypatch, tmp_path):
    """When os.environ has no value, the .env value is loaded."""
    env_file = tmp_path / ".env"
    env_file.write_text('TEST_C6_KEY="from_file"\n')
    monkeypatch.delenv("TEST_C6_KEY", raising=False)
    monkeypatch.setattr(env_loader, "_find_env_file", lambda: env_file)

    result = env_loader.load_env()

    assert result == env_file
    assert os.environ["TEST_C6_KEY"] == "from_file"


def test_load_env_is_idempotent(monkeypatch, tmp_path):
    """A second call is a no-op and does not re-read the file."""
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_C6_IDEM=from_file\n")
    monkeypatch.delenv("TEST_C6_IDEM", raising=False)
    finds = {"n": 0}

    def counting_find():
        finds["n"] += 1
        return env_file

    monkeypatch.setattr(env_loader, "_find_env_file", counting_find)

    first = env_loader.load_env()
    assert first == env_file
    assert os.environ["TEST_C6_IDEM"] == "from_file"

    second = env_loader.load_env()
    assert second is None
    assert finds["n"] == 1  # _find_env_file not called again
    assert os.environ["TEST_C6_IDEM"] == "from_file"


def test_parallel_loads_read_file_once(monkeypatch, tmp_path):
    """Two concurrent calls read the file exactly once and are both safe."""
    from concurrent.futures import ThreadPoolExecutor

    env_file = tmp_path / ".env"
    env_file.write_text("TEST_C6_THREAD=from_file\n")
    monkeypatch.delenv("TEST_C6_THREAD", raising=False)
    finds = {"n": 0}

    def counting_find():
        finds["n"] += 1
        return env_file

    monkeypatch.setattr(env_loader, "_find_env_file", counting_find)

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _: env_loader.load_env(), range(2)))

    assert finds["n"] == 1  # exactly one file read
    assert results.count(env_file) == 1  # one call loaded the file
    assert results.count(None) == 1  # the other was a no-op
    assert os.environ["TEST_C6_THREAD"] == "from_file"


def test_production_does_not_load_file(monkeypatch, tmp_path):
    """In production, .env is not read — even an unset var stays unset."""
    env_file = tmp_path / ".env"
    env_file.write_text("PG_DSN=fromfile\n")
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setattr(env_loader, "_find_env_file", lambda: env_file)

    result = env_loader.load_env()

    assert result is None
    assert "PG_DSN" not in os.environ  # file value NOT loaded in production
