"""Host onboarding slice: disabled-by-default, state machine, hash verification,
heartbeat SLA, scheduler preference with marketplace fallback, RunPod stub."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from backends.base import JobContext
from backends.runpod import RunpodBackend
from gpu_worker import host_registry as hr
from scheduler.host_preference import backend_order


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    monkeypatch.setenv("ROMA_HOSTS_HEARTBEAT_SLA_S", "60")
    return hr.HostRegistry()


def _apply(reg, own=True):
    return reg.apply(
        owner_contact_ref="ref-1", gpu_model="RTX_4090", gpu_count=1, vram_gb=24, region="EU", own_host=own
    )


from gpu_worker import host_bench

TH = {"RTX_4090": {"min_tflops": 1e-9, "max_tflops": 1e9}}  # test-only bounds, not real figures
N = 16


def _start(reg, h, th=TH):
    return reg.start_verification(h.host_id, thresholds=th, n=N, reps=1)


def _verify_ok(reg, h):
    spec = _start(reg, h)
    return reg.submit_verification(h.host_id, spec.job_id, host_bench.reference_hash(spec.seed, N), 0.5)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ROMA_HOSTS_ENABLED", raising=False)
    reg = hr.HostRegistry()
    with pytest.raises(hr.HostsDisabled):
        _apply(reg)
    assert reg.is_schedulable("x") is False


def test_happy_path_own_host_verified(on):
    h = _apply(on)
    assert h.status == hr.PENDING
    h = _verify_ok(on, h)
    assert (h.status, h.trust_tier) == (hr.ACTIVE, "verified")
    assert on.is_schedulable(h.host_id)


def test_external_host_needs_manual_promotion(on):
    h = _verify_ok(on, _apply(on, own=False))
    assert h.trust_tier == "probation" and not on.is_schedulable(h.host_id)
    on.promote(h.host_id)
    assert on.is_schedulable(h.host_id)


def test_hash_mismatch_suspends(on):
    h = _apply(on)
    spec = _start(on, h)
    h = on.submit_verification(h.host_id, spec.job_id, "0" * 64, 0.5)
    assert h.status == hr.SUSPENDED and h.history[-1][2] == "hash mismatch"


@pytest.mark.parametrize("elapsed", [1e6, 1e-30, 0, -1, None])
def test_timing_outside_bounds_suspends(on, elapsed):
    h = _apply(on)
    spec = _start(on, h, th={"RTX_4090": {"min_tflops": 1e-6, "max_tflops": 1e3}})
    h = on.submit_verification(h.host_id, spec.job_id, host_bench.reference_hash(spec.seed, N), elapsed)
    assert h.status == hr.SUSPENDED and h.history[-1][2] == "benchmark outside bounds"


@pytest.mark.parametrize(
    "th", [{}, {"RTX_4090": {"min_tflops": None, "max_tflops": None}}, {"RTX_4090": {"min_tflops": 5, "max_tflops": 1}}]
)
def test_missing_threshold_fails_closed(on, th):
    h = _apply(on)
    with pytest.raises(hr.InvalidTransition):
        on.start_verification(h.host_id, thresholds=th)
    assert on.get(h.host_id).status == hr.PENDING


def test_wrong_job_id_rejected(on):
    h = _apply(on)
    _start(on, h)
    with pytest.raises(hr.InvalidTransition):
        on.submit_verification(h.host_id, "verify_other", "x", 5.0)


def test_illegal_transitions(on):
    h = _apply(on)
    with pytest.raises(hr.InvalidTransition):
        on.promote(h.host_id)
    on.suspend(h.host_id, "manual")
    with pytest.raises(hr.InvalidTransition):
        on.suspend(h.host_id, "again")
    _verify_ok(on, h)  # suspended -> verifying -> active is the only way back
    assert on.get(h.host_id).status == hr.ACTIVE


def test_invalid_hardware(on):
    with pytest.raises(ValueError):
        on.apply(owner_contact_ref="r", gpu_model="", gpu_count=0, vram_gb=0, region="")


def test_heartbeat_sla(on):
    h = _verify_ok(on, _apply(on))
    later = h.last_heartbeat + timedelta(seconds=61)
    assert not on.is_schedulable(h.host_id, now=later)
    assert on.sweep_sla(now=later) == [h.host_id]
    assert on.get(h.host_id).status == hr.SUSPENDED
    assert on.heartbeat(h.host_id) is False


def test_preference_off_by_default(on, monkeypatch):
    monkeypatch.delenv("ROMA_PREFER_OWN_HOSTS", raising=False)
    _verify_ok(on, _apply(on))
    assert backend_order(on) == ["vastai", "runpod"]


def test_preference_on_keeps_fallback(on, monkeypatch):
    monkeypatch.setenv("ROMA_PREFER_OWN_HOSTS", "1")
    h = _verify_ok(on, _apply(on))
    order = backend_order(on)
    assert order == [f"host:{h.host_id}", "vastai", "runpod"]
    assert backend_order(on, min_vram_gb=80) == ["vastai", "runpod"]


def test_preference_registry_error_falls_back(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    monkeypatch.setenv("ROMA_PREFER_OWN_HOSTS", "1")

    class Boom:
        def schedulable_hosts(self):
            raise RuntimeError("db down")

    assert backend_order(Boom()) == ["vastai", "runpod"]


def test_runpod_stub_fail_closed(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "dummy-not-a-secret")
    b = RunpodBackend()
    assert b.enabled is False  # flag missing
    monkeypatch.setenv("ROMA_RUNPOD_ENABLED", "1")
    b = RunpodBackend()
    assert b.enabled is True
    r = asyncio.run(b.dispatch(JobContext(job_id="j1", tenant_id="t1")))
    assert r["status"] == "not_implemented"
    assert b.search_offers() == []
