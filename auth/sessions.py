"""
Session storage for browser-based authentication.
In-memory dict with TTL — resets on server restart.
"""

import time
import uuid
from typing import Optional

# {session_id: {tenant_id, api_key, created_at, expires_at}}
_sessions: dict[str, dict] = {}
SESSION_TTL = 3600  # 1 hour


def _cleanup_expired() -> None:
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if s["expires_at"] < now]
    for sid in expired:
        del _sessions[sid]


def create_session(tenant_id: str, api_key: str) -> str:
    _cleanup_expired()
    session_id = str(uuid.uuid4())
    now = time.time()
    _sessions[session_id] = {
        "tenant_id": tenant_id,
        "api_key": api_key,
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
    return None


def delete_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def active_sessions_count() -> int:
    _cleanup_expired()
    return len(_sessions)
