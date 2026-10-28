"""Deployment constants shared by app code and scripts.

``PUBLIC_BASE_URL`` is the single source of the host the service puts into
links it emits (invite links, verification e-mails, OAuth redirect_uri, the
"request a key at ..." hint). Move it with ``ROMA_PUBLIC_BASE_URL`` instead of
editing every call site; the default keeps the current live host until the
domain decision is made.
"""

from __future__ import annotations

import os

DEFAULT_PUBLIC_BASE_URL = "https://roma-execution-bridge-asurdev.zocomputer.io"

PUBLIC_BASE_URL = os.environ.get("ROMA_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")
