"""ROMA 021b — аренда задачи воркером (shadow по умолчанию).

Колонки привязки (worker_id, lease_token_hash, lease_epoch, lease_expires_at)
добавлены миграцией 021. Здесь только чистая логика: флаг, TTL, выпуск токена
и вердикт «этот ли воркер и не истекла ли аренда».

D-02: исходный токен local-аренды остаётся в серверном реестре; в БД лежит
только sha256. Ответ claim не содержит токен: интеграции не должны ожидать
его от сервера. Токен не пишется в лог/аудит/payload.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import socket
import threading
from datetime import datetime, timedelta, timezone

ENFORCE_ENV = "ROMA_LEASE_ENFORCE"
# D-05: TTL аренды 1800 с. Свипер (JOB_ORPHAN_TIMEOUT, 1200 с) фактическое
# истечение НЕ заменяет: verdict() сам сверяет lease_expires_at с now().
LEASE_TTL_SECONDS = 1800
_TOKEN_BYTES = 32
LOCAL_WORKER_PREFIX = "local:"
# D-06: валидный worker_id — непустая строка без пробелов/управляющих символов.
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,200}$")


def enforce_enabled() -> bool:
    """Блокировать несовпадение аренды. По умолчанию выключено (shadow)."""
    return os.getenv(ENFORCE_ENV, "0").strip().casefold() in ("1", "true", "yes", "on")


def local_worker_id() -> str:
    """Идентичность local-воркера: задача исполняется в процессе сервера."""
    return f"{LOCAL_WORKER_PREFIX}{socket.gethostname() or 'unknown-host'}"


def is_local_binding(job: dict) -> bool:
    bound_to = job.get("worker_id")
    return isinstance(bound_to, str) and bound_to.startswith(LOCAL_WORKER_PREFIX)


def valid_worker_id(worker_id) -> bool:
    return isinstance(worker_id, str) and bool(_WORKER_ID_RE.fullmatch(worker_id))


def binding_state(job: dict) -> str:
    """D-03: 'unbound' | 'bound' | 'unknown' (fail-closed для вызывающего).

    'unknown' — состояние привязки нельзя определить достоверно: в строке нет
    колонки worker_id, тип не тот или строка противоречива (есть хеш токена /
    эпоха > 0 без worker_id).
    """
    if not isinstance(job, dict) or "worker_id" not in job:
        return "unknown"
    bound_to = job.get("worker_id")
    if bound_to is None or bound_to == "":
        try:
            epoch = int(job.get("lease_epoch") or 0)
        except (TypeError, ValueError):
            return "unknown"
        if job.get("lease_token_hash") or epoch > 0:
            return "unknown"
        return "unbound"
    if isinstance(bound_to, str):
        return "bound"
    return "unknown"


# ── D-02: исходный токен local-аренды держит только процесс сервера ─────────
# Токен не уходит клиенту, не пишется в БД (там sha256), лог, аудит и payload.
# Ограничение: реестр в памяти процесса — запрос, пришедший на другую реплику
# (или после рестарта), токена не найдёт и получит no_token (fail-closed в enforce).
_SERVER_LEASES: dict[str, dict] = {}
_SERVER_LEASES_LOCK = threading.Lock()


def remember_server_lease(job_id: str, token: str, epoch, worker_id: str, ttl_seconds: int = LEASE_TTL_SECONDS) -> None:
    now = datetime.now(timezone.utc)
    with _SERVER_LEASES_LOCK:
        for jid in [k for k, v in _SERVER_LEASES.items() if v["forget_after"] <= now]:
            _SERVER_LEASES.pop(jid, None)
        _SERVER_LEASES[job_id] = {
            "token": token,
            "epoch": int(epoch),
            "worker_id": worker_id,
            "forget_after": now + timedelta(seconds=2 * int(ttl_seconds)),
        }


def server_lease(job_id: str) -> dict | None:
    with _SERVER_LEASES_LOCK:
        entry = _SERVER_LEASES.get(job_id)
        return dict(entry) if entry else None


def forget_server_lease(job_id: str) -> None:
    with _SERVER_LEASES_LOCK:
        _SERVER_LEASES.pop(job_id, None)


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


def parse_lease_epoch(value) -> int | None:
    """Parse an integer epoch without truncating a number or accepting a boolean."""
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isinstance(value, str) and parsed != value:
        return None
    return parsed


def verdict(
    job: dict,
    *,
    worker_id: str | None = None,
    lease_token: str | None = None,
    epoch=None,
) -> tuple[bool, str]:
    """(пропустить, причина). Unbound-задачи пропускаются без шума.

    D-06: для bound-задачи worker_id обязателен и должен совпадать с binding;
    эта проверка дополняет, а не заменяет проверку токена и эпохи.
    """
    state = binding_state(job)
    if state == "unbound":
        return True, "unbound"
    if state == "unknown":
        return False, "binding_unknown"
    bound_to = job.get("worker_id")
    if not lease_token or not isinstance(lease_token, str):
        return False, "no_token"
    stored = job.get("lease_token_hash") or ""
    if not stored or not hmac.compare_digest(hash_lease_token(lease_token), stored):
        return False, "bad_token"
    if worker_id is None or worker_id == "":
        return False, "no_worker_id"
    if not valid_worker_id(worker_id):
        return False, "bad_worker_id"
    if worker_id != bound_to:
        return False, "worker_mismatch"
    if epoch is None:
        return False, "no_epoch"
    presented = parse_lease_epoch(epoch)
    expected = parse_lease_epoch(job.get("lease_epoch"))
    if presented is None or expected is None:
        return False, "bad_epoch"
    if presented != expected:
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
