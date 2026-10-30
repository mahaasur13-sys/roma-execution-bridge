"""Framework-agnostic pull API for ROMA host agents. DISABLED unless ROMA_HOSTS_ENABLED=1.

The agent only makes outbound requests; ROMA never connects to the host.
Requests are plain dicts: {"action", "host_id", "token", ...}; responses are
dicts with an HTTP-like "code". Not mounted on any HTTP server yet — a thin
FastAPI/HTTP adapter is a later step.

Security:
  * per-host token: only sha256(token) is stored; comparison via hmac.compare_digest;
    unknown host and wrong token give the same 401 (no host enumeration);
  * single-use job token: every fetch issues a fresh one (old one is invalidated);
    a submit consumes it whether the verification passes or not -> replay = 409;
  * per-host rate limit (ROMA_HOSTS_AGENT_RPM, default 60 requests / 60 s);
  * tokens are never logged or echoed back.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque

from gpu_worker import host_registry as hr

logger = logging.getLogger("roma.hosts.api")

_DUMMY_HASH = hashlib.sha256(b"roma-no-such-host").hexdigest()


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _rpm() -> int:
    try:
        v = int(os.getenv("ROMA_HOSTS_AGENT_RPM", "60"))
    except ValueError:
        return 60
    return v if v > 0 else 60


class HostApi:
    def __init__(self, registry: hr.HostRegistry, clock=time.monotonic) -> None:
        self._reg = registry
        self._clock = clock
        self._mu = threading.Lock()
        self._token_hash: dict[str, str] = {}
        self._job_token_hash: dict[str, tuple[str, str]] = {}  # job_id -> (host_id, sha256)
        self._hits: dict[str, deque] = {}

    # -- operator side ----------------------------------------------------
    def issue_host_token(self, host_id: str) -> str:
        """Returns the plaintext token ONCE; only its hash is kept."""
        if not hr.hosts_enabled():
            raise hr.HostsDisabled("ROMA_HOSTS_ENABLED != 1")
        self._reg.get(host_id)  # KeyError for unknown host
        tok = secrets.token_urlsafe(32)
        with self._mu:
            self._token_hash[host_id] = _h(tok)
        return tok

    def revoke_host_token(self, host_id: str) -> None:
        with self._mu:
            self._token_hash.pop(host_id, None)

    # -- request handling -------------------------------------------------
    def handle(self, req: dict) -> dict:
        if not hr.hosts_enabled():
            return {"code": 503, "error": "hosts_disabled"}
        if not isinstance(req, dict):
            return {"code": 400, "error": "bad_request"}
        host_id = str(req.get("host_id") or "")
        if not self._authenticated(host_id, req.get("token")):
            logger.warning("hosts.api.auth_failed")  # host_id not logged: may be attacker input
            return {"code": 401, "error": "unauthorized"}
        if not self._allow(host_id):
            return {"code": 429, "error": "rate_limited"}
        action = req.get("action")
        try:
            if action == "heartbeat":
                return self._heartbeat(host_id)
            if action == "fetch_job":
                return self._fetch_job(host_id)
            if action == "submit_result":
                return self._submit(host_id, req)
        except (hr.InvalidTransition, ValueError, KeyError) as e:
            return {"code": 409, "error": type(e).__name__}
        return {"code": 400, "error": "unknown_action"}

    def _authenticated(self, host_id: str, token) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._mu:
            stored = self._token_hash.get(host_id)
        ok = hmac.compare_digest(_h(token), stored or _DUMMY_HASH)
        return ok and stored is not None

    def _allow(self, host_id: str) -> bool:
        now = self._clock()
        with self._mu:
            q = self._hits.setdefault(host_id, deque())
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= _rpm():
                return False
            q.append(now)
            return True

    def _heartbeat(self, host_id: str) -> dict:
        h = self._reg.get(host_id)
        accepted = self._reg.heartbeat(host_id)
        return {"code": 200, "status": h.status, "heartbeat_accepted": accepted}

    def _fetch_job(self, host_id: str) -> dict:
        h = self._reg.get(host_id)
        spec = h.pending_verification
        if h.status != hr.VERIFYING or spec is None:
            return {"code": 204, "job": None}
        jt = secrets.token_urlsafe(24)
        with self._mu:
            self._job_token_hash[spec.job_id] = (host_id, _h(jt))
        return {
            "code": 200,
            "job": {
                "type": "verify",
                "job_id": spec.job_id,
                "seed": spec.seed,
                "n": spec.n,
                "reps": spec.reps,
                "job_token": jt,
            },
        }

    def _submit(self, host_id: str, req: dict) -> dict:
        job_id, jt = str(req.get("job_id") or ""), req.get("job_token")
        with self._mu:
            entry = self._job_token_hash.pop(job_id, None)  # single use: consumed now
        valid = (
            entry is not None and entry[0] == host_id and isinstance(jt, str) and hmac.compare_digest(_h(jt), entry[1])
        )
        if not valid:
            return {"code": 409, "error": "invalid_or_used_job_token"}
        h = self._reg.submit_verification(host_id, job_id, str(req.get("output_sha256") or ""), req.get("elapsed_s"))
        return {"code": 200, "status": h.status, "trust_tier": h.trust_tier}
