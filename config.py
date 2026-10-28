"""Deployment constants shared by app code and scripts.

``PUBLIC_BASE_URL`` is the single source of the host the service puts into
links it emits (invite links, verification e-mails, OAuth redirect_uri, the
"request a key at ..." hint). Move it with ``ROMA_PUBLIC_BASE_URL`` instead of
editing every call site; the default keeps the current live host until the
domain decision is made.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

DEFAULT_PUBLIC_BASE_URL = "https://roma-execution-bridge-asurdev.zocomputer.io"

PUBLIC_BASE_URL = os.environ.get("ROMA_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_production() -> bool:
    """Production when ENV/ROMA_ENV says so, or when the public base URL is a non-local https host."""
    if (
        os.environ.get("ENV", "").strip().lower() == "production"
        or os.environ.get("ROMA_ENV", "").strip().lower() == "production"
    ):
        return True
    if not PUBLIC_BASE_URL.lower().startswith("https://"):
        return False
    return (urlparse(PUBLIC_BASE_URL).hostname or "") not in _LOCAL_HOSTS


def cookie_secure() -> bool:
    """Session cookies carry ``Secure`` wherever the service is really served over https."""
    return is_production()
