#!/usr/bin/env python3
"""Operator CLI for ROMA GPU hosts (offline, no HTTP surface). Refuses unless ROMA_HOSTS_ENABLED=1.

Usage:
    python scripts/roma_hosts_admin.py register --host-id felix-3060 --gpu-model RTX_3060 \\
        --gpu-count 1 --vram-gb 12 --owner-label "felix (pop-os)" [--own-host]
    python scripts/roma_hosts_admin.py issue-token --host-id felix-3060 --out ./felix-3060.token
    python scripts/roma_hosts_admin.py start-verification --host-id felix-3060 [--thresholds FILE] [--n 512]
    python scripts/roma_hosts_admin.py approve --host-id felix-3060
    python scripts/roma_hosts_admin.py suspend --host-id felix-3060 --reason "..."
    python scripts/roma_hosts_admin.py revoke-token --host-id felix-3060
    python scripts/roma_hosts_admin.py list

Storage: ROMA_HOSTS_STORE=pg (DSN from PG_DSN) — the only persistent option; the
in-memory store exists for tests. The DSN is never printed unmasked.

Token handling: the plaintext token is written ONLY to the file given by --out,
created with mode 0600 and never overwritten. Only its sha256 is stored. It is
printed to stdout only with --print-once (with a warning on stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpu_worker import host_bench  # noqa: E402
from gpu_worker import host_registry as hr  # noqa: E402
from gpu_worker.host_api import HostApi  # noqa: E402
from gpu_worker.host_store import store_from_env  # noqa: E402


def mask_dsn(dsn: str) -> str:
    if not dsn:
        return "<unset>"
    try:
        u = urlsplit(dsn)
        host = u.hostname or "?"
        port = f":{u.port}" if u.port else ""
        return f"{u.scheme}://***@{host}{port}{u.path or ''}"
    except ValueError:
        return "***"


def write_secret_file(path: str, secret: str) -> None:
    """Create ``path`` with mode 0600; refuse if it already exists (O_EXCL)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (secret + "\n").encode())
    finally:
        os.close(fd)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ROMA hosts operator CLI")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("register", help="register a host (status pending)")
    r.add_argument("--host-id", required=True)
    r.add_argument("--gpu-model", required=True)
    r.add_argument("--gpu-count", type=int, required=True)
    r.add_argument("--vram-gb", type=float, required=True)
    r.add_argument("--owner-label", required=True, help="non-secret label, e.g. a name; no emails/phones")
    r.add_argument("--region", default="")
    r.add_argument("--own-host", action="store_true", help="machine operated by ROMA itself")

    t = sub.add_parser("issue-token", help="issue (or rotate) the host token")
    t.add_argument("--host-id", required=True)
    t.add_argument("--out", help="file to create with mode 0600 (refuses to overwrite)")
    t.add_argument("--print-once", action="store_true", help="also print the token to stdout (unsafe)")

    v = sub.add_parser("start-verification", help="queue a verification job for the host")
    v.add_argument("--host-id", required=True)
    v.add_argument("--thresholds", help="JSON thresholds file (default: ROMA_HOSTS_BENCH_THRESHOLDS)")
    v.add_argument("--n", type=int, default=None)

    for name, hlp in (("revoke-token", "revoke the host token"), ("approve", "approve trust: probation -> verified")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--host-id", required=True)

    s = sub.add_parser("suspend", help="suspend a host")
    s.add_argument("--host-id", required=True)
    s.add_argument("--reason", required=True)

    sub.add_parser("list", help="list hosts (no secrets)")
    return p


def _row(h) -> dict:
    return {
        "host_id": h.host_id,
        "owner_label": h.owner_contact_ref,
        "gpu_model": h.gpu_model,
        "gpu_count": h.gpu_count,
        "vram_gb": h.vram_gb,
        "region": h.region,
        "own_host": h.own_host,
        "status": h.status,
        "trust_tier": h.trust_tier,
        "benchmark_tflops": None if h.benchmark_score is None else round(h.benchmark_score, 3),
        "last_heartbeat": h.last_heartbeat.isoformat() if h.last_heartbeat else None,
    }


def main(argv: list[str] | None = None, store=None) -> int:
    args = _build_parser().parse_args(argv)
    if not hr.hosts_enabled():
        print("refused: ROMA_HOSTS_ENABLED != 1", file=sys.stderr)
        return 2
    try:
        store = store if store is not None else store_from_env()
    except RuntimeError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    kind = "pg" if type(store).__name__ == "PgHostStore" else "memory"
    print(f"store={kind} dsn={mask_dsn(os.getenv('PG_DSN', '')) if kind == 'pg' else '-'}", file=sys.stderr)
    reg = hr.HostRegistry(store=store)
    reg.load_from_store()
    api = HostApi(reg, store=store)

    try:
        if args.command == "register":
            h = reg.apply(
                host_id=args.host_id,
                owner_contact_ref=args.owner_label,
                gpu_model=args.gpu_model,
                gpu_count=args.gpu_count,
                vram_gb=args.vram_gb,
                region=args.region,
                own_host=args.own_host,
            )
            print(json.dumps(_row(h), ensure_ascii=False))
        elif args.command == "issue-token":
            if not args.out and not args.print_once:
                print("refused: give --out FILE (or --print-once)", file=sys.stderr)
                return 2
            if args.out and os.path.exists(args.out):
                print("refused: output file already exists (will not overwrite)", file=sys.stderr)
                return 2
            reg.get(args.host_id)
            tok = api.issue_host_token(args.host_id)
            if args.out:
                write_secret_file(args.out, tok)
                print(f"token for {args.host_id} written to {args.out} (mode 0600); previous token is invalid")
            if args.print_once:
                print(
                    "WARNING: printing the host token once; it is not stored and cannot be shown again", file=sys.stderr
                )
                print(tok)
        elif args.command == "revoke-token":
            reg.get(args.host_id)
            api.revoke_host_token(args.host_id)
            print(f"token for {args.host_id} revoked")
        elif args.command == "start-verification":
            th = host_bench.load_thresholds() if not args.thresholds else _load_th(args.thresholds)
            spec = reg.start_verification(args.host_id, thresholds=th, n=args.n)
            print(
                json.dumps(
                    {
                        "host_id": args.host_id,
                        "job_id": spec.job_id,
                        "n": spec.n,
                        "reps": spec.reps,
                        "min_tflops": spec.min_tflops,
                        "max_tflops": spec.max_tflops,
                    }
                )
            )
        elif args.command == "approve":
            print(json.dumps(_row(reg.promote(args.host_id)), ensure_ascii=False))
        elif args.command == "suspend":
            print(json.dumps(_row(reg.suspend(args.host_id, args.reason)), ensure_ascii=False))
        elif args.command == "list":
            for h in sorted(reg._hosts.values(), key=lambda x: x.host_id):
                print(json.dumps(_row(h), ensure_ascii=False))
    except KeyError:
        print("error: unknown host", file=sys.stderr)
        return 1
    except (ValueError, hr.InvalidTransition) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _load_th(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


if __name__ == "__main__":
    sys.exit(main())
