"""start_verification must not hold the registry lock during the O(n^3) reference hash."""

from __future__ import annotations

import threading

import pytest

from gpu_worker import host_bench
from gpu_worker import host_registry as hr

TH = {"RTX_4090": {"min_tflops": 1e-9, "max_tflops": 1e9}}  # test-only bounds


@pytest.fixture
def reg(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    return hr.HostRegistry()


def _apply(reg):
    return reg.apply(owner_contact_ref="r", gpu_model="RTX_4090", gpu_count=1, vram_gb=24, region="EU")


def _blocking_hash(monkeypatch, entered, release, fail=False):
    real = host_bench.reference_hash

    def slow(seed, n):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test release not signalled")
        if fail:
            raise RuntimeError("hash failed")
        return real(seed, n)

    monkeypatch.setattr(host_bench, "reference_hash", slow)


def _run(fn, box):
    def target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - test captures any outcome
            box["error"] = e

    t = threading.Thread(target=target)
    t.start()
    return t


def test_lock_not_held_during_hash(reg, monkeypatch):
    h, other = _apply(reg), _apply(reg)
    entered, release = threading.Event(), threading.Event()
    _blocking_hash(monkeypatch, entered, release)
    box: dict = {}
    t = _run(lambda: reg.start_verification(h.host_id, thresholds=TH, n=4, reps=1), box)
    assert entered.wait(5)
    assert reg._mu.acquire(timeout=1)  # lock is free while the hash runs
    reg._mu.release()
    reg.suspend(other.host_id, "unrelated op during hash")
    assert reg.get(h.host_id).status == hr.VERIFYING
    release.set()
    t.join(5)
    assert "error" not in box and box["result"].n == 4
    assert reg.get(h.host_id).pending_verification is box["result"]
    assert reg.get(h.host_id).verify_reservation is None


def test_two_concurrent_starts_only_one_wins(reg, monkeypatch):
    h = _apply(reg)
    entered, release = threading.Event(), threading.Event()
    _blocking_hash(monkeypatch, entered, release)
    box: dict = {}
    t = _run(lambda: reg.start_verification(h.host_id, thresholds=TH, n=4, reps=1), box)
    assert entered.wait(5)
    with pytest.raises(hr.InvalidTransition):
        reg.start_verification(h.host_id, thresholds=TH, n=4, reps=1)
    release.set()
    t.join(5)
    assert "result" in box


def test_reservation_released_on_hash_error(reg, monkeypatch):
    h = _apply(reg)
    entered, release = threading.Event(), threading.Event()
    _blocking_hash(monkeypatch, entered, release, fail=True)
    release.set()
    with pytest.raises(RuntimeError):
        reg.start_verification(h.host_id, thresholds=TH, n=4, reps=1)
    rec = reg.get(h.host_id)
    assert rec.status == hr.PENDING and rec.verify_reservation is None and rec.pending_verification is None
    assert rec.history[-1] == (hr.VERIFYING, hr.PENDING, "verification reservation released")
    monkeypatch.undo()
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    assert reg.start_verification(h.host_id, thresholds=TH, n=4, reps=1).n == 4  # retry works


def test_suspend_during_hash_invalidates_reservation(reg, monkeypatch):
    h = _apply(reg)
    entered, release = threading.Event(), threading.Event()
    _blocking_hash(monkeypatch, entered, release)
    box: dict = {}
    t = _run(lambda: reg.start_verification(h.host_id, thresholds=TH, n=4, reps=1), box)
    assert entered.wait(5)
    reg.suspend(h.host_id, "operator")
    release.set()
    t.join(5)
    assert isinstance(box.get("error"), hr.InvalidTransition)
    rec = reg.get(h.host_id)
    assert rec.status == hr.SUSPENDED and rec.pending_verification is None


@pytest.mark.parametrize("bad", [0, -1, host_bench.MAX_EXACT_N + 1, True, 2.5, "8"])
def test_invalid_n_rejected_before_reservation(reg, bad):
    h = _apply(reg)
    with pytest.raises(ValueError):
        reg.start_verification(h.host_id, thresholds=TH, n=bad, reps=1)
    assert reg.get(h.host_id).status == hr.PENDING


def test_env_n_is_clamped(reg, monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_BENCH_N", str(host_bench.MAX_EXACT_N * 4))
    monkeypatch.setattr(host_bench, "DEFAULT_N", 4)
    h = _apply(reg)
    assert reg.start_verification(h.host_id, thresholds=TH, reps=1).n == 4
