#!/usr/bin/env python3
"""Safe command execution for ROMA workers.

Security hardening (deny-all / explicit-allow) for local task execution.

Replaces the previous `subprocess.run(task, shell=True)` pattern with:
  1. Rejection of any shell metacharacters (no shell interpretation, no injection).
  2. Parsing with `shlex.split` and execution WITHOUT a shell (`shell=False`).
  3. A strict allowlist of executables — anything not listed is refused.

The allowlist can be overridden without editing code via the
`ROMA_ALLOWED_COMMANDS` environment variable (comma-separated basenames).

Explicitly NOT allowlisted (and therefore denied by default): interactive
shells (`bash`, `sh`, `zsh`), container/runtime control (`docker`, `kubectl`),
and destructive filesystem commands. Add them deliberately only if a sandbox
(bwrap/Landlock) is enforced around the worker process.
"""

from __future__ import annotations

import os
import shlex
import subprocess

# Shell metacharacters that MUST NOT appear in a task. Their presence means the
# task is a shell fragment and would be interpreted by a shell — so it is
# rejected outright instead of ever reaching one.
_SHELL_META = set(';|&`$()[]{}<>!\\\n\r\t')

# Read-only diagnostics + safe tooling + nvidia-smi (the canonical smoke test).
# This is intentionally conservative: deny-all, explicit-allow.
_DEFAULT_ALLOWED = (
    "nvidia-smi", "echo", "date", "ls", "cat", "uname", "whoami", "id", "pwd",
    "hostname", "env", "printenv", "lscpu", "lsmem", "lsusb", "lspci",
    "ps", "top", "df", "du", "free", "uptime", "python", "python3",
)

ALLOWED_COMMANDS = frozenset(
    c.strip()
    for c in os.environ.get("ROMA_ALLOWED_COMMANDS", ",".join(_DEFAULT_ALLOWED)).split(",")
    if c.strip()
)


class CommandNotAllowed(Exception):
    """Raised when a task is not on the allowlist or contains shell metachars."""


def _allowed(cmd: str) -> bool:
    """Return True if the command basename is explicitly allowlisted."""
    basename = os.path.basename(cmd)
    return basename in ALLOWED_COMMANDS


def validate_command(task: str) -> list[str]:
    """Validate a task and return its argv (no shell). Raises CommandNotAllowed."""
    task = (task or "").strip()
    if not task:
        raise CommandNotAllowed("empty command")

    # 1) No shell metacharacters — block shell injection entirely.
    if any(ch in _SHELL_META for ch in task):
        raise CommandNotAllowed("shell metacharacters are not allowed")

    # 2) Parse into argv without a shell.
    try:
        argv = shlex.split(task)
    except ValueError as exc:
        raise CommandNotAllowed(f"unparseable command: {exc}") from exc

    if not argv:
        raise CommandNotAllowed("empty command")

    # 3) Deny-all, explicit-allow: only allowlisted executables may run.
    if not _allowed(argv[0]):
        raise CommandNotAllowed(f"command not allowlisted: {argv[0]!r}")

    return argv


def run_command_checked(task: str, timeout: int = 300) -> dict:
    """Validate and run a single command without a shell.

    Returns ``{"ok": bool, "out": str, "err": str, "code": int}``.
    Raises :class:`CommandNotAllowed` if the command fails validation.
    """
    argv = validate_command(task)
    try:
        r = subprocess.run(
            argv, shell=False, capture_output=True, text=True, timeout=timeout
        )
        return {"ok": r.returncode == 0, "out": r.stdout, "err": r.stderr, "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": f"timeout after {timeout}s", "code": 124}
    except Exception as exc:  # noqa: BLE001 — worker must never crash on a task
        return {"ok": False, "out": "", "err": str(exc), "code": -1}
