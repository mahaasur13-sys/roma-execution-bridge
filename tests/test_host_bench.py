"""Deterministic matmul verification: reference (numpy / pure Python) and optional CUDA path."""

from __future__ import annotations

import json

import pytest

from gpu_worker import host_bench as hb


def test_inputs_deterministic_and_in_range():
    a1, b1 = hb.make_inputs("seed-x", 8)
    a2, b2 = hb.make_inputs("seed-x", 8)
    assert (a1, b1) == (a2, b2) and len(a1) == 64
    assert all(-8 <= v <= 8 for v in a1 + b1)
    assert hb.make_inputs("seed-y", 8)[0] != a1


def test_reference_hash_stable_and_seed_sensitive():
    assert hb.reference_hash("s1", 12) == hb.reference_hash("s1", 12)
    assert hb.reference_hash("s1", 12) != hb.reference_hash("s2", 12)
    assert hb.reference_hash("s1", 12) != hb.reference_hash("s1", 13)


def test_numpy_and_pure_python_agree(monkeypatch):
    """If numpy is installed, its path must equal the pure-Python path (CI has no numpy: both pure)."""
    first = hb.reference_hash("agree", 10)
    import builtins

    real = builtins.__import__

    def no_numpy(name, *a, **k):
        if name == "numpy":
            raise ImportError
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_numpy)
    assert hb.reference_hash("agree", 10) == first


def test_float32_accumulation_is_exact():
    """Every partial sum, rounded to float32 at each step (worst-case GPU accumulation), stays exact."""
    import struct

    def f32(x):
        return struct.unpack("<f", struct.pack("<f", x))[0]

    n = 48
    a, b = hb.make_inputs("f32", n)
    flat = []
    for i in range(n):
        for j in range(n):
            acc = 0.0
            for k in range(n):
                acc = f32(acc + f32(a[i * n + k] * b[k * n + j]))
            flat.append(int(round(acc)))
    assert hb._hash_result(n, struct.pack(f"<{n*n}i", *flat)) == hb.reference_hash("f32", n)
    assert 64 * hb.MAX_EXACT_N < 2**24 + 1


def test_tflops_and_bench_n(monkeypatch):
    assert hb.tflops(1000, 1, 2.0) == pytest.approx(1e-3)
    monkeypatch.setenv("ROMA_HOSTS_BENCH_N", "999999999")
    assert hb.bench_n() == hb.DEFAULT_N
    monkeypatch.setenv("ROMA_HOSTS_BENCH_N", "abc")
    assert hb.bench_n() == hb.DEFAULT_N


def test_thresholds_loader(tmp_path, monkeypatch):
    monkeypatch.delenv("ROMA_HOSTS_BENCH_THRESHOLDS", raising=False)
    assert hb.load_thresholds() == {}
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"_note": "x", "M": {"min_tflops": 1, "max_tflops": 2}}))
    monkeypatch.setenv("ROMA_HOSTS_BENCH_THRESHOLDS", str(f))
    assert hb.load_thresholds() == {"M": {"min_tflops": 1, "max_tflops": 2}}
    f.write_text("{broken")
    assert hb.load_thresholds() == {}


def test_shipped_example_has_no_numbers():
    from pathlib import Path

    data = json.loads((Path(__file__).resolve().parent.parent / "config/host_bench_thresholds.example.json").read_text())
    assert all(v["min_tflops"] is None and v["max_tflops"] is None for k, v in data.items() if not k.startswith("_"))
