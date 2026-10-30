"""Deterministic GPU verification benchmark for ROMA hosts.

Workload: C = A @ B, where A, B are n×n matrices of small integers in [-8, 8]
generated from a fresh ROMA seed with a portable SHA-256 counter PRNG (not
numpy/torch RNG, so every platform produces identical inputs).

Why the result is exactly reproducible (tolerance = 0):
  * every product is an integer with |a*b| <= 64 and every partial sum is an
    integer with |sum| <= 64*n. For n <= 2**18 that stays below 2**24, so the
    float32 accumulation is EXACT regardless of summation order, kernel, GPU
    model or TF32 (inputs of magnitude <= 8 are exact in TF32's 10-bit mantissa).
  * the result is still rounded to int32 before hashing as a belt-and-braces
    quantisation step. Any difference therefore means wrong/absent compute.

What it proves: the host actually performed this specific n×n matmul for this
seed (it cannot be precomputed), and gives a self-reported timing.
What it does NOT prove: the exact GPU model/VRAM, that a GPU (vs CPU) did it,
or that the timing is honest (elapsed_s is reported by the host; a cheater can
only be caught by the plausibility upper bound and by later job telemetry).
"""

from __future__ import annotations

import hashlib
import json
import re
import os
import struct
import time

VALUE_RANGE = 8  # inputs in [-8, 8]
DEFAULT_N = 512
DEFAULT_REPS = 20
MAX_EXACT_N = 2**18  # 64*n < 2**24


def bench_n() -> int:
    try:
        n = int(os.getenv("ROMA_HOSTS_BENCH_N", str(DEFAULT_N)))
    except ValueError:
        return DEFAULT_N
    return n if 1 <= n <= MAX_EXACT_N else DEFAULT_N


def _values(seed: str, label: str, count: int) -> list[int]:
    out: list[int] = []
    ctr = 0
    span = 2 * VALUE_RANGE + 1
    while len(out) < count:
        d = hashlib.sha256(f"{seed}|{label}|{ctr}".encode()).digest()
        out.extend(b % span - VALUE_RANGE for b in d)  # 255 % 17 == 0 -> unbiased
        ctr += 1
    return out[:count]


def make_inputs(seed: str, n: int) -> tuple[list[int], list[int]]:
    return _values(seed, "A", n * n), _values(seed, "B", n * n)


def _hash_result(n: int, flat_int32) -> str:
    h = hashlib.sha256(b"roma-bench-v1|" + struct.pack("<I", n))
    h.update(flat_int32)
    return h.hexdigest()


def reference_hash(seed: str, n: int) -> str:
    """ROMA-side expected hash. numpy if installed, otherwise pure Python."""
    a, b = make_inputs(seed, n)
    try:
        import numpy as np

        A = np.asarray(a, dtype=np.int64).reshape(n, n)
        B = np.asarray(b, dtype=np.int64).reshape(n, n)
        C = (A @ B).astype("<i4")
        return _hash_result(n, C.tobytes())
    except ImportError:
        rows = []
        for i in range(n):
            ai = a[i * n : (i + 1) * n]
            rows.append([sum(ai[k] * b[k * n + j] for k in range(n)) for j in range(n)])
        flat = struct.pack(f"<{n * n}i", *(v for r in rows for v in r))
        return _hash_result(n, flat)


def run_host_bench(seed: str, n: int, reps: int = DEFAULT_REPS, device: str = "cuda") -> dict:
    """Host-side runner (what the roma-bench image executes). Requires torch."""
    import torch

    a, b = make_inputs(seed, n)
    dev = torch.device(device)
    torch.backends.cuda.matmul.allow_tf32 = False
    A = torch.tensor(a, dtype=torch.float32, device=dev).reshape(n, n)
    B = torch.tensor(b, dtype=torch.float32, device=dev).reshape(n, n)
    C = A @ B
    if dev.type == "cuda":
        torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(reps):
        C = A @ B
    if dev.type == "cuda":
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0
    flat = torch.round(C).to(torch.int32).cpu().numpy().astype("<i4").tobytes()
    return {
        "output_sha256": _hash_result(n, flat),
        "elapsed_s": elapsed,
        "reps": reps,
        "device": torch.cuda.get_device_name(dev) if dev.type == "cuda" else "cpu",
    }


def tflops(n: int, reps: int, elapsed_s: float) -> float:
    return 2.0 * n**3 * reps / elapsed_s / 1e12


def normalize_gpu_model(name: str) -> str:
    """Canonical form for threshold lookup: case-insensitive, runs of spaces,
    hyphens and underscores are equivalent ('RTX 3060' == 'RTX_3060' == 'rtx-3060')."""
    if not isinstance(name, str):
        return ""
    return "_".join(re.split(r"[\s_\-]+", name.strip().lower())).strip("_")


def lookup_threshold(thresholds: dict, gpu_model: str) -> dict:
    """Threshold entry for gpu_model. Exact key wins; otherwise a unique match on
    normalize_gpu_model. Unknown model or ambiguous normalized match => {} (fail closed)."""
    exact = thresholds.get(gpu_model)
    if isinstance(exact, dict):
        return exact
    key = normalize_gpu_model(gpu_model)
    if not key:
        return {}
    hits = [v for k, v in thresholds.items() if isinstance(v, dict) and normalize_gpu_model(k) == key]
    if len(hits) != 1:
        return {}
    return hits[0]


def load_thresholds() -> dict:
    """Per-GPU-model {min_tflops, max_tflops}. Operator-provided; shipped EMPTY.

    Source: JSON file at ROMA_HOSTS_BENCH_THRESHOLDS. Missing/invalid => {} and
    verification fails closed ("no threshold configured")."""
    path = os.getenv("ROMA_HOSTS_BENCH_THRESHOLDS", "")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}
    except (OSError, ValueError):
        return {}
