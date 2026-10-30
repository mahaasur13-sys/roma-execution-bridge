"""Manual live check on a machine with torch + CUDA (not part of pytest: no GPU in CI).

Usage: python scripts/host_bench_selftest.py [n] [reps]
Prints whether the CUDA result hash equals ROMA's CPU reference, and timing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpu_worker import host_bench as hb  # noqa: E402

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 512
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    r = hb.run_host_bench("selftest-seed", n, reps=reps)
    ok = r["output_sha256"] == hb.reference_hash("selftest-seed", n)
    print(
        f"device={r['device']} n={n} reps={reps} match={ok} "
        f"elapsed_s={r['elapsed_s']:.4f} tflops={hb.tflops(n, reps, r['elapsed_s']):.3f}"
    )
    sys.exit(0 if ok else 1)
