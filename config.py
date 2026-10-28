"""Deployment constants shared by app code and scripts.

``PUBLIC_BASE_URL`` is the single source of the host the service puts into
links it emits (invite links, verification e-mails, OAuth redirect_uri, the
"request a key at ..." hint). Move it with ``ROMA_PUBLIC_BASE_URL`` instead of
editing every call site; the default keeps the current live host until the
domain decision is made.
"""

from __future__ import annotations

import os
import re
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


_PREFIX_RE = re.compile(r"^/[A-Za-z0-9/_-]*$")


def url_prefix() -> str:
    """Mount point the service is proxied under, from ``ROMA_URL_PREFIX``.

    Live is exposed through ``https://asurdev.zo.space/roma``, so every link and
    redirect the app emits has to carry the same prefix the proxy strips. Empty
    by default: served at the root, links unchanged.
    """
    raw = os.environ.get("ROMA_URL_PREFIX", "").strip()
    if not raw or raw == "/":
        return ""
    if not raw.startswith("/"):
        raw = "/" + raw
    raw = raw.rstrip("/")
    if "//" in raw or not _PREFIX_RE.match(raw):
        raise ValueError("ROMA_URL_PREFIX must look like /roma: '/', letters, digits, '_', '-' only, got %r" % raw)
    return raw


def u(path: str) -> str:
    """Prefix a root-relative path (``/dashboard``) for the current mount point."""
    return url_prefix() + path


# Validated at import so a bad prefix fails the start-up, not the first click.
url_prefix()
