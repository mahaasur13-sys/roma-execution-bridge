"""ROMA 021b — аренда задачи воркером (shadow по умолчанию).

Колонки привязки (worker_id, lease_token_hash, lease_epoch, lease_expires_at)
добавлены миграцией 021. Здесь только чистая логика: флаг, TTL, выпуск токена
и вердикт «этот ли воркер и не истекла ли аренда».

Токен не секрет БД: в колонке лежит sha256, исходное значение уходит воркеру
один раз в ответе выдачи задачи и никогда не пишется в лог/аудит/payload.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
from datetime import datetime, timezone

ENFORCE_ENV = "ROMA_LEASE_ENFORCE"
LEASE_TTL_SECONDS = 1800
_TOKEN_BYTES = 32


def enforce_enabled() -> bool:
    """Блокировать несовпадение аренды. По умолчанию выключено (shadow)."""
    return os.getenv(ENFORCE_ENV, "0").strip().casefold() in ("1", "true", "yes", "on")


def local_worker_id() -> str:
    """Идентичность local-воркера: задача исполняется в процессе сервера."""
    return f"local:{socket.gethostname() or 'unknown-host'}"


def new_lease_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_lease_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _as_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def verdict(
    job: dict,
    *,
    worker_id: str | None = None,
    lease_token: str | None = None,
    epoch=None,
) -> tuple[bool, str]:
    """(пропустить, причина). Unbound-задачи всегда пропускаются без шума."""
    bound_to = job.get("worker_id")
    if not bound_to:
        return True, "unbound"
    if not lease_token:
        return False, "no_token"
    stored = job.get("lease_token_hash") or ""
    if not stored or not hmac.compare_digest(hash_lease_token(lease_token), stored):
        return False, "bad_token"
    if worker_id and worker_id != bound_to:
        return False, "worker_mismatch"
    if epoch is not None:
        try:
            presented = int(epoch)
        except (TypeError, ValueError):
            return False, "bad_epoch"
        if presented != int(job.get("lease_epoch") or 0):
            return False, "stale_epoch"
    expires_at = _as_utc(job.get("lease_expires_at"))
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        return False, "expired"
    return True, "ok"


def audit_data(job: dict, reason: str) -> dict:
    """Событие аудита без токена: только идентификаторы и причины."""
    return {
        "reason": reason,
        "enforce": enforce_enabled(),
        "worker_id": job.get("worker_id"),
        "lease_epoch": job.get("lease_epoch"),
        "lease_expires_at": str(job.get("lease_expires_at") or ""),
        "status": job.get("status"),
    }
