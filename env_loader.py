"""Unified .env loader (C6).

Loads a ``.env`` file into ``os.environ`` exactly once per process.

Rules:
- Environment variables already set take precedence over ``.env`` values
  (``os.environ.setdefault`` — env wins over file).
- Secret values are never logged (only the resolved path, at debug level).
- A missing ``.env`` is not an error.
- ``load_env()`` is idempotent: repeated calls are safe no-ops.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("roma.env")

_loaded = False


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

    Returns the loaded path, or ``None`` if no file was found (or already loaded).
    """
    global _loaded
    if _loaded:
        return None
    _loaded = True  # mark first so a missing file is a cheap no-op on retry

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
