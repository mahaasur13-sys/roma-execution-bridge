"""Public base URL resolution (config.PUBLIC_BASE_URL) and CORS alias fallback.

Module-level constants are read at import time, so the "does every call site use
ROMA_PUBLIC_BASE_URL" cases run in a fresh subprocess with a controlled env
instead of reloading half the app inside the test session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

import config
import main
from deps import verify_api_key as deps_verify_api_key

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDE = "https://app.example.test/base"
_ENV_KEYS = ("ROMA_PUBLIC_BASE_URL", "VERIFICATION_BASE_URL", "OAUTH_REDIRECT_BASE")

_PROBE = """
import json
import routers.admin
import routers.auth
import config

print(json.dumps({
    "config": config.PUBLIC_BASE_URL,
    "admin": routers.admin.BASE_URL,
    "verification": routers.auth.VERIFICATION_BASE_URL,
    "oauth": routers.auth.OAUTH_REDIRECT_BASE,
}))
"""


def _probe(**env_override: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(env_override)
    res = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _all_hosts(values: dict[str, str]) -> set[str]:
    return set(values.values())


def test_override_reaches_every_call_site():
    values = _probe(ROMA_PUBLIC_BASE_URL=OVERRIDE + "/")
    assert _all_hosts(values) == {OVERRIDE}


def test_default_when_env_absent():
    values = _probe()
    assert _all_hosts(values) == {config.DEFAULT_PUBLIC_BASE_URL}


def test_explicit_env_still_wins_over_public_base_url():
    values = _probe(
        ROMA_PUBLIC_BASE_URL=OVERRIDE,
        VERIFICATION_BASE_URL="https://verify.example.test",
        OAUTH_REDIRECT_BASE="https://oauth.example.test",
    )
    assert values["config"] == OVERRIDE
    assert values["admin"] == OVERRIDE
    assert values["verification"] == "https://verify.example.test"
    assert values["oauth"] == "https://oauth.example.test"


@pytest.mark.parametrize("verify", [deps_verify_api_key, main.verify_api_key])
def test_missing_key_hint_uses_public_base_url(verify):
    with pytest.raises(HTTPException) as exc:
        verify(None)
    assert exc.value.status_code == 401
    assert config.PUBLIC_BASE_URL in exc.value.detail


def _clean_env(monkeypatch):
    for key in ("CORS_ALLOW_ORIGINS", "CORS_ORIGINS", "ENV", "ROMA_ENV"):
        monkeypatch.delenv(key, raising=False)
    # the default PUBLIC_BASE_URL is a public https host, which reads as production
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "http://localhost:8000")


def test_cors_origins_fallback(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CORS_ORIGINS", "http://a.example, http://b.example")
    assert main._cors_allowed_origins() == ["http://a.example", "http://b.example"]


def test_cors_allow_origins_has_priority(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://allow.example")
    monkeypatch.setenv("CORS_ORIGINS", "http://legacy.example")
    assert main._cors_allowed_origins() == ["http://allow.example"]


def test_cors_both_unset_keeps_previous_default(monkeypatch):
    _clean_env(monkeypatch)
    assert main._cors_allowed_origins() == ["http://127.0.0.1:3080", "http://localhost:3080"]


def test_no_hardcoded_host_in_code():
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in ("tests", ".git") or rel == Path("config.py"):
            continue
        if "zocomputer.io" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(rel))
    assert offenders == []
