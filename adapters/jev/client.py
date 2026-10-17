"""adapters/jev/client.py — backward-compat ре-экспорт канонического клиента.

Клиент перенесён в ``core.jev_client``. Этот модуль оставлен для sidecar-адаптера
(``adapters/jev/adapter.py``, ``run.py``) и его проверок: все публичные имена
ре-экспортируются из ядра, чтобы существующие импорты не менялись.
"""

from __future__ import annotations

from core.jev_client import (  # noqa: F401
    DEFAULT_MODEL,
    ENDPOINT,
    LIMITS,
    JevBadResponse,
    JevClient,
    JevDisabled,
    JevError,
    JevRateLimited,
    JevTimeout,
    api_key,
    http_transport,
    is_enabled,
    mask,
)

__all__ = [
    "DEFAULT_MODEL",
    "ENDPOINT",
    "LIMITS",
    "JevBadResponse",
    "JevClient",
    "JevDisabled",
    "JevError",
    "JevRateLimited",
    "JevTimeout",
    "api_key",
    "http_transport",
    "is_enabled",
    "mask",
]
