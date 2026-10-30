"""fix/host-bench-gpu-model-key: threshold lookup normalizes GPU model names."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpu_worker import host_bench
from gpu_worker import host_registry as hr

TH = {"RTX_3060": {"min_tflops": 1e-9, "max_tflops": 1e9}}  # test-only bounds


@pytest.mark.parametrize("model", ["RTX_3060", "RTX 3060", "rtx-3060", "  Rtx  3060 ", "rtx__3060", "RTX - 3060"])
def test_lookup_equivalent_spellings(model):
    assert host_bench.lookup_threshold(TH, model) is TH["RTX_3060"]


@pytest.mark.parametrize("model", ["RTX 3070", "RTX3060", "", "3060", None])
def test_lookup_unknown_is_empty(model):
    assert host_bench.lookup_threshold(TH, model) == {}


def test_exact_key_wins_and_ambiguous_fails_closed():
    a, b = {"min_tflops": 1, "max_tflops": 2}, {"min_tflops": 3, "max_tflops": 4}
    th = {"RTX_3060": a, "rtx-3060": b}
    assert host_bench.lookup_threshold(th, "RTX_3060") is a
    assert host_bench.lookup_threshold(th, "rtx-3060") is b
    assert host_bench.lookup_threshold(th, "RTX 3060") == {}  # ambiguous => fail closed


def test_shipped_config_matches_spaced_model():
    cfg = Path(__file__).resolve().parents[1] / "config" / "host_bench_thresholds.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    th = {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
    t = host_bench.lookup_threshold(th, "RTX 3060")
    assert (t["min_tflops"], t["max_tflops"]) == (3.5, 13.0)


@pytest.fixture
def reg(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    return hr.HostRegistry()


def _apply(reg, model):
    return reg.apply(owner_contact_ref="ref-1", gpu_model=model, gpu_count=1, vram_gb=12, region="EU", own_host=True)


def test_registry_start_verification_with_spaced_model(reg):
    h = _apply(reg, "RTX 3060")
    spec = reg.start_verification(h.host_id, thresholds=TH, n=16, reps=1)
    assert spec.job_id
    assert reg._hosts[h.host_id].status == hr.VERIFYING


def test_registry_unknown_model_still_errors(reg):
    h = _apply(reg, "RTX 3070")
    with pytest.raises(hr.InvalidTransition, match="no benchmark threshold"):
        reg.start_verification(h.host_id, thresholds=TH, n=16, reps=1)
    assert reg._hosts[h.host_id].status == hr.PENDING
