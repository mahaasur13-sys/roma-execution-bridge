"""core.jev_observer checks (hermetic — без реальных потоков).

Named ``jev_observer_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide canon
run (``.ci/run-completeness.json``, ``collected == 486``) must not shift. Run explicitly:

    .venv/bin/python -m pytest core/jev_observer_checks.py -q
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.jev_observer as observer  # noqa: E402


def test_submit_drops_when_queue_full(monkeypatch):
    monkeypatch.setattr(observer._slots, "acquire", lambda blocking=False: False)
    submitted: list = []

    class _FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(observer, "_get_executor", lambda: _FakeExecutor())
    assert observer.submit_observation(print, 1) is False
    assert submitted == []  # при переполнении submit вообще не вызывается


def test_submit_returns_false_on_submit_error(monkeypatch):
    monkeypatch.setattr(observer._slots, "acquire", lambda blocking=False: True)
    releases: list = []
    monkeypatch.setattr(observer._slots, "release", lambda: releases.append(1))

    class _FakeExecutor:
        def submit(self, fn, *args):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(observer, "_get_executor", lambda: _FakeExecutor())
    assert observer.submit_observation(print, 1) is False  # не бросает
    assert len(releases) == 1  # слот вернулся ровно один раз


def test_submit_semaphore_budget_cycle(monkeypatch):
    """Бюджет 1: submit → True, второй → False, после done-callback → снова True."""
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(observer, "_slots", slots)
    futures: list = []

    class _Future:
        def __init__(self):
            self._cb = None

        def add_done_callback(self, cb):
            self._cb = cb

        def complete(self):
            if self._cb is not None:
                self._cb(self)

    class _FakeExecutor:
        def submit(self, fn, *args):
            fut = _Future()
            futures.append(fut)
            return fut

    monkeypatch.setattr(observer, "_get_executor", lambda: _FakeExecutor())

    assert observer.submit_observation(print, 1) is True   # слот занят
    assert observer.submit_observation(print, 2) is False  # бюджет 1 → drop
    futures[0].complete()                                   # done-callback → release
    assert observer.submit_observation(print, 3) is True   # слот снова свободен


def test_submit_succeeds_when_slot_available(monkeypatch):
    monkeypatch.setattr(observer._slots, "acquire", lambda blocking=False: True)
    monkeypatch.setattr(observer._slots, "release", lambda: None)
    submitted: list = []

    class _Future:
        def add_done_callback(self, cb):
            pass

    class _FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn, args))
            return _Future()

    monkeypatch.setattr(observer, "_get_executor", lambda: _FakeExecutor())
    assert observer.submit_observation(print, 1, 2) is True
    assert len(submitted) == 1
    assert submitted[0][1] == (1, 2)
