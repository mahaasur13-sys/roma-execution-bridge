"""ROMA external host registry (phase-1 slice, DISABLED by default).

Scope: application -> verification -> trust tier -> heartbeat SLA.
NO payout / money logic lives here by design (see out/HOSTS_PLAN.md).

Fail-closed rules:
  * Feature flag ROMA_HOSTS_ENABLED must be exactly "1"; otherwise every
    mutating call raises HostsDisabled.
  * Only whitelisted state transitions are allowed.
  * A host becomes `active` only through a verification result whose
    output hash matches the expected hash AND whose benchmark passes the floor.
  * Missed heartbeat SLA => host is not schedulable (and is suspended on sweep).
"""

from __future__ import annotations

import hmac
import os
import threading
import uuid
from dataclasses import dataclass, field
from gpu_worker import host_bench
from datetime import datetime, timedelta, timezone

PENDING, VERIFYING, ACTIVE, SUSPENDED = "pending", "verifying", "active", "suspended"
STATES = (PENDING, VERIFYING, ACTIVE, SUSPENDED)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({VERIFYING, SUSPENDED}),
    VERIFYING: frozenset({ACTIVE, SUSPENDED}),
    ACTIVE: frozenset({SUSPENDED}),
    SUSPENDED: frozenset({VERIFYING}),  # re-verification is the only way back
}

TRUST_TIERS = ("none", "probation", "verified")


class HostsDisabled(RuntimeError):
    pass


class InvalidTransition(ValueError):
    pass


def hosts_enabled() -> bool:
    return os.getenv("ROMA_HOSTS_ENABLED", "0").strip() == "1"


def heartbeat_sla_seconds() -> int:
    try:
        v = int(os.getenv("ROMA_HOSTS_HEARTBEAT_SLA_S", "60"))
    except ValueError:
        return 60
    return v if v > 0 else 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class VerificationJobSpec:
    """Seeded integer matmul (see gpu_worker/host_bench.py); exact hash, tolerance 0."""

    job_id: str
    host_id: str
    image: str
    command: str
    seed: str
    n: int
    reps: int
    expected_sha256: str
    min_tflops: float
    max_tflops: float
    timeout_s: int = 900


@dataclass
class HostRecord:
    host_id: str
    owner_contact_ref: str  # opaque reference, never a secret
    gpu_model: str
    gpu_count: int
    vram_gb: float
    region: str
    own_host: bool = False  # True = machines operated by ROMA itself (phase 2 pilot)
    status: str = PENDING
    trust_tier: str = "none"
    benchmark_score: float | None = None
    last_heartbeat: datetime | None = None
    created_at: datetime = field(default_factory=_now)
    history: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, reason)
    pending_verification: VerificationJobSpec | None = None
    verify_reservation: str | None = None  # token while the reference hash is computed


class HostRegistry:
    def __init__(self) -> None:
        self._hosts: dict[str, HostRecord] = {}
        self._mu = threading.Lock()

    @staticmethod
    def _guard() -> None:
        if not hosts_enabled():
            raise HostsDisabled("ROMA_HOSTS_ENABLED != 1")

    def _transition(self, h: HostRecord, to: str, reason: str) -> None:
        if to not in ALLOWED_TRANSITIONS.get(h.status, frozenset()):
            raise InvalidTransition(f"{h.status} -> {to} not allowed")
        h.history.append((h.status, to, reason))
        h.status = to

    # -- application ------------------------------------------------------
    def apply(
        self,
        *,
        owner_contact_ref: str,
        gpu_model: str,
        gpu_count: int,
        vram_gb: float,
        region: str,
        own_host: bool = False,
    ) -> HostRecord:
        self._guard()
        if gpu_count < 1 or vram_gb <= 0 or not gpu_model.strip():
            raise ValueError("invalid hardware declaration")
        rec = HostRecord(
            host_id=f"host_{uuid.uuid4().hex[:12]}",
            owner_contact_ref=owner_contact_ref,
            gpu_model=gpu_model.strip(),
            gpu_count=gpu_count,
            vram_gb=vram_gb,
            region=region,
            own_host=own_host,
        )
        with self._mu:
            self._hosts[rec.host_id] = rec
        return rec

    def get(self, host_id: str) -> HostRecord:
        return self._hosts[host_id]

    # -- verification -----------------------------------------------------
    def start_verification(
        self,
        host_id: str,
        *,
        thresholds: dict | None = None,
        image: str = "roma/host-bench:pinned",
        n: int | None = None,
        reps: int = host_bench.DEFAULT_REPS,
    ) -> VerificationJobSpec:
        """Fail-closed: no configured {min,max}_tflops for the declared model => InvalidTransition.

        Locking: the O(n^3) reference hash is computed OUTSIDE ``self._mu``.
          1. under the lock: validate thresholds / n / state, move the host to
             VERIFYING and store a reservation token (a second caller now fails);
          2. without the lock: compute the reference hash;
          3. under the lock: the token must still match and the host must still be
             VERIFYING, then the spec is stored. On any error in step 2 the
             reservation is released and the previous status restored.
        """
        self._guard()
        th = thresholds if thresholds is not None else host_bench.load_thresholds()
        n = host_bench.bench_n() if n is None else n
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= host_bench.MAX_EXACT_N:
            raise ValueError(f"n must be an int in [1, {host_bench.MAX_EXACT_N}]")
        token = uuid.uuid4().hex
        with self._mu:
            h = self._hosts[host_id]
            t = th.get(h.gpu_model) or {}
            lo, hi = t.get("min_tflops"), t.get("max_tflops")
            if not (isinstance(lo, int | float) and isinstance(hi, int | float) and 0 < lo < hi):
                raise InvalidTransition(f"no benchmark threshold configured for {h.gpu_model}")
            prev_status = h.status
            self._transition(h, VERIFYING, "verification reserved")  # raises if already verifying
            h.verify_reservation = token
            h.pending_verification = None

        seed = uuid.uuid4().hex  # fresh per run: host can't precompute
        try:
            expected = host_bench.reference_hash(seed, n)
        except BaseException:
            with self._mu:
                if h.verify_reservation == token and h.status == VERIFYING:
                    h.history.append((VERIFYING, prev_status, "verification reservation released"))
                    h.status = prev_status
                if h.verify_reservation == token:
                    h.verify_reservation = None
            raise

        with self._mu:
            if h.verify_reservation != token or h.status != VERIFYING:
                raise InvalidTransition("verification reservation lost (host changed state)")
            spec = VerificationJobSpec(
                job_id=f"verify_{uuid.uuid4().hex[:12]}",
                host_id=host_id,
                image=image,
                command=f"roma-bench --seed {seed} --n {n} --reps {reps}",
                seed=seed,
                n=n,
                reps=reps,
                expected_sha256=expected,
                min_tflops=float(lo),
                max_tflops=float(hi),
            )
            h.verify_reservation = None
            h.pending_verification = spec
            return spec

    def submit_verification(self, host_id: str, job_id: str, output_sha256: str, elapsed_s: float) -> HostRecord:
        self._guard()
        with self._mu:
            h = self._hosts[host_id]
            spec = h.pending_verification
            if h.status != VERIFYING or spec is None or spec.job_id != job_id:
                raise InvalidTransition("no matching verification in progress")
            h.pending_verification = None
            ok_hash = hmac.compare_digest(str(output_sha256 or ""), spec.expected_sha256)
            score = None
            if isinstance(elapsed_s, int | float) and elapsed_s > 0:
                score = host_bench.tflops(spec.n, spec.reps, float(elapsed_s))
            ok_score = score is not None and spec.min_tflops <= score <= spec.max_tflops
            if ok_hash and ok_score:
                h.benchmark_score = score
                h.trust_tier = "verified" if h.own_host else "probation"
                h.last_heartbeat = _now()
                self._transition(h, ACTIVE, "verification passed")
            else:
                h.trust_tier = "none"
                self._transition(h, SUSPENDED, "hash mismatch" if not ok_hash else "benchmark outside bounds")
            return h

    def promote(self, host_id: str) -> HostRecord:
        """Manual operator step: probation -> verified (no automatic promotion)."""
        self._guard()
        with self._mu:
            h = self._hosts[host_id]
            if h.status != ACTIVE:
                raise InvalidTransition("only active hosts can be promoted")
            h.trust_tier = "verified"
            return h

    def suspend(self, host_id: str, reason: str) -> HostRecord:
        self._guard()
        with self._mu:
            h = self._hosts[host_id]
            self._transition(h, SUSPENDED, reason)
            h.trust_tier = "none"
            h.verify_reservation = None
            h.pending_verification = None
            return h

    # -- heartbeat SLA ----------------------------------------------------
    def heartbeat(self, host_id: str) -> bool:
        self._guard()
        with self._mu:
            h = self._hosts.get(host_id)
            if h is None or h.status != ACTIVE:
                return False
            h.last_heartbeat = _now()
            return True

    def is_schedulable(self, host_id: str, now: datetime | None = None) -> bool:
        if not hosts_enabled():
            return False
        h = self._hosts.get(host_id)
        if h is None or h.status != ACTIVE or h.trust_tier != "verified" or h.last_heartbeat is None:
            return False
        return ((now or _now()) - h.last_heartbeat) <= timedelta(seconds=heartbeat_sla_seconds())

    def sweep_sla(self, now: datetime | None = None) -> list[str]:
        self._guard()
        now = now or _now()
        out = []
        with self._mu:
            for h in self._hosts.values():
                if h.status == ACTIVE and (
                    h.last_heartbeat is None or now - h.last_heartbeat > timedelta(seconds=heartbeat_sla_seconds())
                ):
                    self._transition(h, SUSPENDED, "heartbeat SLA missed")
                    h.trust_tier = "none"
                    out.append(h.host_id)
        return out

    def schedulable_hosts(self, now: datetime | None = None) -> list[HostRecord]:
        return [h for h in self._hosts.values() if self.is_schedulable(h.host_id, now)]
