"""ROMA host agent MVP. Stdlib only; torch is needed only to run the GPU benchmark.

Outbound only: the agent POSTs JSON to ROMA; it opens no ports.
Runs ONLY verification jobs (type == "verify"). Any other job type is refused:
real workloads stay disabled until container isolation (gVisor/Kata) exists.

Config (env, or a KEY=VALUE file via --config):
  ROMA_URL              base URL of ROMA's host agent API, e.g. https://roma.example/hosts/agent
  ROMA_HOST_ID          host id issued by the operator
  ROMA_HOST_TOKEN_FILE  path to a file holding the host token (preferred)
  ROMA_HOST_TOKEN       token (fallback; avoid: visible in the process env)
  ROMA_AGENT_POLL_S     poll interval, default 30
  ROMA_AGENT_DEVICE     torch device for the benchmark, default "cuda"
  ROMA_TOKEN_HEADER     "authorization" (default: Authorization: Bearer <token>) or
                        "x-roma-host-token" (token only in X-Roma-Host-Token; for fronts
                        that strip Authorization and translate it back, e.g. zo.space)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("roma.host_agent")

ALLOWED_JOB_TYPES = frozenset({"verify"})

TOKEN_HEADER_CHOICES = ("authorization", "x-roma-host-token")
DEFAULT_TOKEN_HEADER = "authorization"

# Explicit User-Agent: Cloudflare in front of zo.space answers 403 to the
# default "Python-urllib/3.x" UA, so the agent identifies itself instead.
AGENT_VERSION = "0.1"
USER_AGENT = f"roma-host-agent/{AGENT_VERSION}"


def _check_token_header(value: str) -> str:
    if value not in TOKEN_HEADER_CHOICES:
        raise ValueError(f"token header must be one of {', '.join(TOKEN_HEADER_CHOICES)}")
    return value


@dataclass
class AgentConfig:
    url: str
    host_id: str
    token: str = field(repr=False)
    poll_s: float = 30.0
    device: str = "cuda"
    token_header: str = "authorization"

    def __repr__(self) -> str:  # never show the token
        return f"AgentConfig(url={self.url!r}, host_id={self.host_id!r}, token=***, poll_s={self.poll_s})"


def _read_kv(path: str) -> dict:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def load_config(config_file: str | None = None, env: dict | None = None) -> AgentConfig:
    src = dict(env if env is not None else os.environ)
    if config_file:
        src = {**_read_kv(config_file), **{k: v for k, v in src.items() if k.startswith("ROMA_")}}
    token = ""
    if src.get("ROMA_HOST_TOKEN_FILE"):
        token = Path(src["ROMA_HOST_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    token = token or src.get("ROMA_HOST_TOKEN", "")
    missing = [
        k
        for k, v in (
            ("ROMA_URL", src.get("ROMA_URL")),
            ("ROMA_HOST_ID", src.get("ROMA_HOST_ID")),
            ("ROMA_HOST_TOKEN", token),
        )
        if not v
    ]
    if missing:
        raise SystemExit(f"config missing: {', '.join(missing)}")
    return AgentConfig(
        url=src["ROMA_URL"],
        host_id=src["ROMA_HOST_ID"],
        token=token,
        poll_s=float(src.get("ROMA_AGENT_POLL_S", "30")),
        device=src.get("ROMA_AGENT_DEVICE", "cuda"),
        token_header=_token_header_from(src),
    )


def _token_header_from(src: dict) -> str:
    try:
        return _check_token_header(src.get("ROMA_TOKEN_HEADER", DEFAULT_TOKEN_HEADER) or DEFAULT_TOKEN_HEADER)
    except ValueError as e:
        raise SystemExit(f"config invalid: ROMA_TOKEN_HEADER: {e}") from None


ROUTES = {"heartbeat": "/heartbeat", "fetch_job": "/job", "submit_result": "/result"}


def _urllib_post(url: str, headers: dict, data: bytes, timeout: float) -> tuple[int, bytes]:
    headers = dict(headers)
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = USER_AGENT
    r = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:  # nosec B310 - URL from operator config
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_transport(base_url: str, timeout: float = 20.0, post=None, token_header: str = DEFAULT_TOKEN_HEADER):
    """Outbound HTTPS transport to ROMA's /hosts/agent/* routes.

    base_url example: https://roma.example/hosts/agent. ``post`` is injectable for tests.
    The token is never logged. ``token_header="authorization"`` (default) sends it as
    ``Authorization: Bearer <token>``; ``"x-roma-host-token"`` sends it only as
    ``X-Roma-Host-Token: <token>`` (no Authorization header) for a front that strips
    Authorization and translates the header back; ROMA's server contract is unchanged.
    """
    _check_token_header(token_header)
    post = post or _urllib_post
    base = base_url.rstrip("/")

    def send(req: dict) -> dict:
        req = dict(req)
        action = req.pop("action")
        token = req.pop("token", "")
        if token_header == "x-roma-host-token":
            headers = {
                "Content-Type": "application/json",
                "X-Roma-Host-Token": str(token),
                "X-Roma-Host-Id": str(req.pop("host_id", "")),
                "User-Agent": USER_AGENT,
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Roma-Host-Id": str(req.pop("host_id", "")),
                "User-Agent": USER_AGENT,
            }
        status, raw = post(base + ROUTES[action], headers, json.dumps(req).encode(), timeout)
        if status == 204:
            return {"code": 204, "job": None}
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {"error": "bad_response"}
        if not isinstance(body, dict):
            body = {"error": "bad_response"}
        return {**body, "code": status}

    return send


def default_bench(seed: str, n: int, reps: int, device: str) -> dict:
    from gpu_worker.host_bench import run_host_bench

    return run_host_bench(seed, n, reps=reps, device=device)


class HostAgent:
    def __init__(self, cfg: AgentConfig, transport, bench=default_bench, dry_run: bool = False) -> None:
        self.cfg, self._send, self._bench, self.dry_run = cfg, transport, bench, dry_run

    def _call(self, action: str, **kw) -> dict:
        return self._send({"action": action, "host_id": self.cfg.host_id, "token": self.cfg.token, **kw})

    def run_once(self) -> dict:
        """One cycle: heartbeat -> fetch -> (verify job) -> submit. Returns a summary (no secrets)."""
        hb = self._call("heartbeat")
        if hb.get("code") != 200:
            logger.warning("heartbeat rejected code=%s error=%s", hb.get("code"), hb.get("error"))
            return {"stage": "heartbeat", "code": hb.get("code"), "error": hb.get("error")}
        resp = self._call("fetch_job")
        job = resp.get("job") if resp.get("code") == 200 else None
        if not job:
            return {"stage": "idle", "status": hb.get("status")}
        if job.get("type") not in ALLOWED_JOB_TYPES:
            logger.warning("refusing job type=%s (only verification jobs are allowed)", job.get("type"))
            return {"stage": "refused", "job_type": job.get("type")}
        logger.info("verification job %s n=%s reps=%s", job["job_id"], job["n"], job["reps"])
        if self.dry_run:
            return {"stage": "dry_run", "job_id": job["job_id"], "n": job["n"], "reps": job["reps"]}
        r = self._bench(job["seed"], int(job["n"]), int(job["reps"]), self.cfg.device)
        sub = self._call(
            "submit_result",
            job_id=job["job_id"],
            job_token=job["job_token"],
            output_sha256=r["output_sha256"],
            elapsed_s=r["elapsed_s"],
        )
        logger.info("submitted job %s -> code=%s status=%s", job["job_id"], sub.get("code"), sub.get("status"))
        return {
            "stage": "submitted",
            "job_id": job["job_id"],
            "code": sub.get("code"),
            "status": sub.get("status"),
            "trust_tier": sub.get("trust_tier"),
        }

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 - keep polling; never log request bodies
                logger.warning("cycle failed: %s", type(e).__name__)
            time.sleep(self.cfg.poll_s)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="ROMA host agent (verification jobs only)")
    p.add_argument("--config", help="KEY=VALUE file")
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="fetch the job but do not run or submit it")
    p.add_argument(
        "--token-header",
        choices=TOKEN_HEADER_CHOICES,
        default=None,
        help="how to send the token: authorization (default) or x-roma-host-token (env ROMA_TOKEN_HEADER)",
    )
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    token_header = a.token_header or cfg.token_header
    agent = HostAgent(cfg, http_transport(cfg.url, token_header=token_header), dry_run=a.dry_run)
    if a.once or a.dry_run:
        print(json.dumps(agent.run_once()))
        return 0
    agent.run_forever()
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
