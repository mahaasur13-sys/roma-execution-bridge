"""core.jev_cascade checks (hermetic — без сети).

Named ``jev_cascade_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide canon
run (``.ci/run-completeness.json``) must not shift. Run explicitly:

    .venv/bin/python -m pytest core/jev_cascade_checks.py -q
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.jev.client import JevError, JevTimeout  # noqa: E402
from core.jev_cascade import safe_cascade  # noqa: E402

import pytest  # noqa: E402


def test_returns_result_when_fn_succeeds():
    assert safe_cascade("base", lambda a, b=0: a + b, 2, b=3, label="ok") == 5


def test_returns_baseline_on_exception(caplog):
    def _boom():
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.WARNING, logger="roma.jev.cascade"):
        assert safe_cascade({"action": "allow"}, _boom, label="policy.cascade_once") == {
            "action": "allow"
        }
    assert "policy.cascade_once" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "kaboom" not in caplog.text  # ни деталей исключения, ни данных запроса


@pytest.mark.parametrize("error", [JevError("network"), JevTimeout("slow"), ValueError("x")])
def test_returns_baseline_on_any_error(error):
    def _boom():
        raise error

    assert safe_cascade(None, _boom, label="triage.cascade_once") is None


def test_baseline_object_returned_by_identity():
    sentinel = object()

    def _boom():
        raise RuntimeError("x")

    assert safe_cascade(sentinel, _boom, label="x") is sentinel


def test_propagates_cancelled_error():
    def _cancel():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        safe_cascade(None, _cancel, label="policy.cascade_once")
