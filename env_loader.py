"""Unified .env loader (C6).

Contract (kept for CodeRabbit review):
1. Process environment always wins over the file — ``os.environ.setdefault``,
   so any variable already present in the environment is never overwritten.
2. In production (``ENV=production`` or ``ROMA_ENV=production``) the ``.env``
   file is **not** read at all — env-only mode (secrets come from the
   platform, never from a local file).
3. Outside production the ``.env`` is applied as an overlay, and values are
   never logged (only the resolved path, at debug level).

``load_env()`` is idempotent and thread-safe: repeated and concurrent calls
read the file at most once and are safe no-ops afterwards.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("roma.env")

_loaded = False
_lock = threading.Lock()


def _is_production() -> bool:
    """True when the environment is marked production (env-only loading)."""
    env = os.environ.get("ENV", "").strip().lower()
    roma_env = os.environ.get("ROMA_ENV", "").strip().lower()
    return env == "production" or roma_env == "production"


def _find_env_file() -> Path | None:
    """Return the ``.env`` path (cwd first, then the repo root) or None."""
    repo_root = Path(__file__).resolve().parent
    candidates = (Path.cwd() / ".env", repo_root / ".env")
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


def load_env() -> Path | None:
    """Load ``.env`` into ``os.environ`` without overriding existing vars.

    Returns the loaded path, or ``None`` if no file was loaded (already loaded,
    production mode, or missing file).
    """
    global _loaded
    with _lock:
        if _loaded:
            return None
        _loaded = True  # mark first so a missing file is a cheap no-op on retry

        if _is_production():
            return None  # env-only in production — never read the file

        env_path = _find_env_file()
        if env_path is None:
            return None

        try:
            with open(env_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key:
                        os.environ.setdefault(key, value)
        except OSError:
            logger.warning("Failed to read env file: %s", env_path)
            return None

        logger.debug("Loaded env from %s", env_path)
        return env_path
