"""Invite code management for closed beta."""

import os
import secrets
from datetime import datetime, timedelta, timezone

import db_adapter as db


BETA_MODE = os.environ.get("BETA_MODE", "false").lower() in ("1", "true", "yes")
BETA_MAX_USERS = int(os.environ.get("BETA_MAX_USERS", "100"))
BETA_REQUIRE_INVITE = os.environ.get("BETA_REQUIRE_INVITE", "true").lower() in ("1", "true", "yes")
BETA_DEFAULT_SPEND_CAP = float(os.environ.get("BETA_DEFAULT_SPEND_CAP", "5.00"))


def generate_invite_code(prefix: str = "ROMA") -> str:
    part = secrets.token_hex(4).upper()
    return f"{prefix}-{part[:4]}-{part[4:8]}"


def create_invite(max_uses: int = 1, note: str = "", expires_hours: int = 0, created_by: str = "admin") -> dict:
    if not BETA_MODE:
        return {"error": "Beta mode is not enabled. Set BETA_MODE=true in .env"}
    code = generate_invite_code()
    expires_at = None
    if expires_hours > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()
    return db.create_invite_code(code, created_by, max_uses, note, expires_at)


def validate_invite(code: str) -> dict | None:
    if not BETA_MODE:
        return {"valid": True, "beta_disabled": True}
    if not BETA_REQUIRE_INVITE:
        return {"valid": True, "invites_not_required": True}
    invite = db.validate_invite_code(code)
    if not invite:
        return None
    return {"valid": True, "invite": invite}


def use_invite(code: str, user_id: str) -> bool:
    invite = db.validate_invite_code(code)
    if not invite:
        return False
    db.use_invite_code(invite["id"], user_id)
    return True


def list_invites() -> list[dict]:
    return db.list_invite_codes()


def deactivate_invite(code: str) -> bool:
    return db.deactivate_invite_code(code)


def check_beta_capacity() -> dict:
    if not BETA_MODE:
        return {"allowed": True, "beta_disabled": True}
    cfg = db.get_beta_config()
    user_count = db.count_verified_users()
    max_users = cfg.get("max_users", BETA_MAX_USERS)
    is_active = cfg.get("is_active", True)
    return {
        "allowed": is_active and user_count < max_users,
        "beta_active": is_active,
        "current_users": user_count,
        "max_users": max_users,
        "slots_remaining": max(0, max_users - user_count),
    }


def is_beta_enabled() -> bool:
    return BETA_MODE and db.get_beta_config().get("is_active", True)
