"""Email verification service for ROMA Execution Bridge."""

import secrets
import logging
from datetime import datetime, timedelta, timezone

import db_adapter as db

VERIFICATION_EXPIRY_HOURS = 24
logger = logging.getLogger("roma.verification")


def generate_token() -> str:
    """Generate a URL-safe verification token (32 bytes)."""
    return secrets.token_urlsafe(32)


def create_verification(user_id: str) -> tuple[str, str]:
    """Generate a new verification token. Returns (token, expires_at_iso)."""
    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_EXPIRY_HOURS)
    expires_at_iso = expires_at.isoformat()
    db.update_verification_token(user_id, token, expires_at_iso)
    return token, expires_at_iso


def verify_token(token: str) -> tuple:
    """Validate a verification token.
    Returns (result_dict_or_None, reason) where reason is 'success', 'invalid', or 'expired'.
    """
    user = db.find_user_by_verification_token(token)
    if not user:
        logger.warning(
            "verification_token_invalid",
            extra={"token_prefix": token[:8] if token else ""},
        )
        return None, "invalid"

    expires_at = user.get("verification_token_expires_at")
    if expires_at is not None:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            logger.warning(
                "verification_token_expired",
                extra={
                    "user_id": user["id"],
                    "email": user.get("email", ""),
                    "expires_at": str(expires_at),
                },
            )
            return None, "expired"

    return {"user_id": user["id"], "email": user.get("email")}, "success"


def is_email_verified(api_key: str) -> bool:
    """Check if user associated with this API key has verified their email."""
    user = db.find_user_by_api_key(api_key)
    if not user:
        return False
    return user.get("email_verified", False)
