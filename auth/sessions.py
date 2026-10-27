"""
Session storage for browser-based authentication.
In-memory dict with TTL — resets on server restart.

CSRF tokens live in a parallel map keyed by session id (``_csrf``): the session
record itself keeps its original shape, so nothing that reads ``_sessions``
learns a new field.
"""

import hmac
import secrets
import time
import uuid
from typing import Optional

# {session_id: {tenant_id, created_at, expires_at}}
_sessions: dict[str, dict] = {}
# {session_id: csrf_token} — dropped together with its session
_csrf: dict[str, str] = {}
SESSION_TTL = 3600  # 1 hour


def _cleanup_expired() -> None:
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if s["expires_at"] < now]
    for sid in expired:
        del _sessions[sid]
        _csrf.pop(sid, None)


def create_session(tenant_id: str) -> str:
    _cleanup_expired()
    session_id = str(uuid.uuid4())
    now = time.time()
    _sessions[session_id] = {
        "tenant_id": tenant_id,
        "created_at": now,
        "expires_at": now + SESSION_TTL,
    }
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    _cleanup_expired()
    s = _sessions.get(session_id)
    if s and s["expires_at"] > time.time():
        return s
    if session_id in _sessions:
        del _sessions[session_id]
        _csrf.pop(session_id, None)
    return None


def delete_session(session_id: str) -> None:
    _sessions.pop(session_id, None)
    _csrf.pop(session_id, None)


def csrf_token_for(session_id: str) -> str:
    """Per-session CSRF token — minted once, reused for the session's lifetime."""
    token = _csrf.get(session_id)
    if not token:
        token = secrets.token_urlsafe(32)
        _csrf[session_id] = token
    return token


def csrf_valid(session_id: str, token: Optional[str]) -> bool:
    """Constant-time check of a submitted token against the session's token."""
    expected = _csrf.get(session_id)
    if not expected or not token:
        return False
    return hmac.compare_digest(expected, token)


def active_sessions_count() -> int:
    _cleanup_expired()
    return len(_sessions)
