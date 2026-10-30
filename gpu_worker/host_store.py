"""Persistence for the host registry and host API.

MemoryHostStore — default, no I/O (tests, single process).
PgHostStore     — PostgreSQL write-through (migrations 019 + 020). Selected with
                  ROMA_HOSTS_STORE=pg; DSN from PG_DSN. The DSN is never logged.

Only hashes of host tokens and job tokens are stored. Job-token consumption is a
single atomic UPDATE ... WHERE used_at IS NULL, so a token cannot be used twice
even across processes. The rest of the registry is write-through from one process
(the in-memory registry stays authoritative while running and is reloaded from
PG on start).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone


class MemoryHostStore:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self.token_hash: dict[str, str] = {}
        self.job_tokens: dict[str, tuple[str, str]] = {}

    # registry hooks (no-op in memory)
    def save_host(self, h, new_events) -> None:
        return None

    def start_verification(self, spec) -> None:
        return None

    def finish_verification(self, job_id, reported_sha256, score, elapsed_s, passed) -> None:
        return None

    def load_hosts(self) -> list:
        return []

    # tokens
    def load_host_tokens(self) -> dict[str, str]:
        with self._mu:
            return dict(self.token_hash)

    def set_host_token(self, host_id: str, token_sha256: str) -> None:
        with self._mu:
            self.token_hash[host_id] = token_sha256

    def revoke_host_token(self, host_id: str) -> None:
        with self._mu:
            self.token_hash.pop(host_id, None)

    def put_job_token(self, job_id: str, host_id: str, token_sha256: str) -> None:
        with self._mu:
            self.job_tokens[job_id] = (host_id, token_sha256)

    def consume_job_token(self, job_id: str) -> tuple[str, str] | None:
        with self._mu:
            return self.job_tokens.pop(job_id, None)


class PgHostStore:
    """psycopg2 store. ``connect`` is injectable for tests."""

    def __init__(self, dsn: str | None = None, connect=None) -> None:
        self._dsn = dsn if dsn is not None else os.getenv("PG_DSN", "")
        if not self._dsn and connect is None:
            raise RuntimeError("PgHostStore: PG_DSN is not set")
        if connect is None:
            import psycopg2

            connect = psycopg2.connect
        self._connect = connect

    def __repr__(self) -> str:
        return "PgHostStore(dsn=***)"

    def _run(self, sql: str, args=(), fetch: str | None = None):
        conn = self._connect(self._dsn)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, args)
                    if fetch == "one":
                        return cur.fetchone()
                    if fetch == "all":
                        return cur.fetchall()
                    return None
        finally:
            conn.close()

    def save_host(self, h, new_events) -> None:
        conn = self._connect(self._dsn)
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO hosts (host_id, owner_contact_ref, gpu_model, gpu_count, vram_gb, region,
                               own_host, status, trust_tier, benchmark_score, last_heartbeat, created_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (host_id) DO UPDATE SET status=EXCLUDED.status,
                               trust_tier=EXCLUDED.trust_tier, benchmark_score=EXCLUDED.benchmark_score,
                               last_heartbeat=EXCLUDED.last_heartbeat""",
                        (
                            h.host_id,
                            h.owner_contact_ref,
                            h.gpu_model,
                            h.gpu_count,
                            h.vram_gb,
                            h.region,
                            h.own_host,
                            h.status,
                            h.trust_tier,
                            h.benchmark_score,
                            h.last_heartbeat,
                            h.created_at,
                        ),
                    )
                    for frm, to, reason in new_events:
                        cur.execute(
                            "INSERT INTO host_status_events (host_id, from_status, to_status, reason) "
                            "VALUES (%s,%s,%s,%s)",
                            (h.host_id, frm, to, reason),
                        )
        finally:
            conn.close()

    def start_verification(self, spec) -> None:
        self._run(
            """INSERT INTO host_verifications (job_id, host_id, seed, expected_sha256, n, reps, min_tflops,
                   max_tflops, image) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                spec.job_id,
                spec.host_id,
                spec.seed,
                spec.expected_sha256,
                spec.n,
                spec.reps,
                spec.min_tflops,
                spec.max_tflops,
                spec.image,
            ),
        )

    def finish_verification(self, job_id, reported_sha256, score, elapsed_s, passed) -> None:
        self._run(
            """UPDATE host_verifications SET reported_sha256=%s, score=%s, elapsed_s=%s, passed=%s,
                   completed_at=now() WHERE job_id=%s AND passed IS NULL""",
            (reported_sha256, score, elapsed_s if isinstance(elapsed_s, int | float) else None, passed, job_id),
        )

    def load_hosts(self) -> list:
        from gpu_worker.host_registry import VERIFYING, HostRecord, VerificationJobSpec

        rows = self._run(
            """SELECT host_id, owner_contact_ref, gpu_model, gpu_count, vram_gb, region, own_host, status,
                      trust_tier, benchmark_score, last_heartbeat, created_at FROM hosts""",
            fetch="all",
        )
        out = []
        for r in rows or []:
            h = HostRecord(
                host_id=r[0],
                owner_contact_ref=r[1],
                gpu_model=r[2],
                gpu_count=r[3],
                vram_gb=float(r[4]),
                region=r[5],
                own_host=r[6],
                status=r[7],
                trust_tier=r[8],
                benchmark_score=r[9],
                last_heartbeat=r[10],
                created_at=r[11] or datetime.now(timezone.utc),
            )
            if h.status == VERIFYING:
                v = self._run(
                    """SELECT job_id, seed, expected_sha256, n, reps, min_tflops, max_tflops, image
                         FROM host_verifications WHERE host_id=%s AND passed IS NULL
                        ORDER BY created_at DESC LIMIT 1""",
                    (h.host_id,),
                    fetch="one",
                )
                if v and v[3] is not None:
                    h.pending_verification = VerificationJobSpec(
                        job_id=v[0],
                        host_id=h.host_id,
                        image=v[7] or "",
                        seed=v[1],
                        n=v[3],
                        reps=v[4],
                        command=f"roma-bench --seed {v[1]} --n {v[3]} --reps {v[4]}",
                        expected_sha256=v[2],
                        min_tflops=v[5],
                        max_tflops=v[6],
                    )
            out.append(h)
        return out

    def load_host_tokens(self) -> dict[str, str]:
        rows = self._run("SELECT host_id, token_sha256 FROM host_tokens WHERE revoked_at IS NULL", fetch="all")
        return {r[0]: r[1] for r in rows or []}

    def set_host_token(self, host_id: str, token_sha256: str) -> None:
        self._run(
            """INSERT INTO host_tokens (host_id, token_sha256) VALUES (%s,%s)
               ON CONFLICT (host_id) DO UPDATE SET token_sha256=EXCLUDED.token_sha256, created_at=now(),
                   revoked_at=NULL""",
            (host_id, token_sha256),
        )

    def revoke_host_token(self, host_id: str) -> None:
        self._run("UPDATE host_tokens SET revoked_at=now() WHERE host_id=%s AND revoked_at IS NULL", (host_id,))

    def put_job_token(self, job_id: str, host_id: str, token_sha256: str) -> None:
        self._run(
            """INSERT INTO host_job_tokens (job_id, host_id, token_sha256) VALUES (%s,%s,%s)
               ON CONFLICT (job_id) DO UPDATE SET token_sha256=EXCLUDED.token_sha256, host_id=EXCLUDED.host_id,
                   issued_at=now(), used_at=NULL""",
            (job_id, host_id, token_sha256),
        )

    def consume_job_token(self, job_id: str) -> tuple[str, str] | None:
        r = self._run(
            """UPDATE host_job_tokens SET used_at=now() WHERE job_id=%s AND used_at IS NULL
               RETURNING host_id, token_sha256""",
            (job_id,),
            fetch="one",
        )
        return (r[0], r[1]) if r else None


def store_from_env():
    return PgHostStore() if os.getenv("ROMA_HOSTS_STORE", "memory").strip() == "pg" else MemoryHostStore()
