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

from prometheus_client import REGISTRY  # noqa: E402


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

    assert observer.submit_observation(print, 1) is True  # слот занят
    assert observer.submit_observation(print, 2) is False  # бюджет 1 → drop
    futures[0].complete()  # done-callback → release
    assert observer.submit_observation(print, 3) is True  # слот снова свободен


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


# ── Метрика дропов (дефолтный registry, сравнение дельт) ────────────────


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class _Delta:
    """Дельта метрики до/после блока: значения живут в общем registry между тестами."""

    def __init__(self, name: str, **labels: str) -> None:
        self._name = name
        self._labels = labels

    def __enter__(self) -> "_Delta":
        self._before = _sample(self._name, **self._labels)
        return self

    def __exit__(self, *exc: object) -> bool:
        self._after = _sample(self._name, **self._labels)
        return False

    @property
    def value(self) -> float:
        return self._after - self._before


def test_queue_full_increments_dropped_metric(monkeypatch):
    monkeypatch.setattr(observer._slots, "acquire", lambda blocking=False: False)
    with _Delta("roma_jev_shadow_dropped_total", point="R1") as d:
        assert observer.submit_observation(print, 1, point="R1") is False
    assert d.value == 1.0


def test_submit_error_increments_dropped_metric(monkeypatch):
    monkeypatch.setattr(observer._slots, "acquire", lambda blocking=False: True)
    monkeypatch.setattr(observer._slots, "release", lambda: None)

    class _FakeExecutor:
        def submit(self, fn, *args):
            raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(observer, "_get_executor", lambda: _FakeExecutor())
    with _Delta("roma_jev_shadow_dropped_total", point="R5") as d:
        assert observer.submit_observation(print, 1, point="R5") is False
    assert d.value == 1.0


def test_unknown_point_collapses_to_other(monkeypatch):
    monkeypatch.setattr(observer._slots, "acquire", lambda blocking=False: False)
    with _Delta("roma_jev_shadow_dropped_total", point="other") as d:
        assert observer.submit_observation(print, 1, point="R99") is False
    assert d.value == 1.0


def test_drop_metric_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(observer._slots, "acquire", lambda blocking=False: False)

    def _boom(*args, **kwargs):
        raise RuntimeError("registry down")

    monkeypatch.setattr(observer, "track_jev_shadow", _boom)
    assert observer.submit_observation(print, 1, point="R1") is False
