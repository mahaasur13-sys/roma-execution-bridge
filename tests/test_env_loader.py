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
