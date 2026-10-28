"""Маскирование секретов в access-логе.

uvicorn.access печатает полный путь запроса вместе с query-строкой:
``GET /auth/verify-email?token=<token> HTTP/1.1``. В access-лог попадали
токены подтверждения адреса и API-ключи, переданные в query. Фильтр ниже
маскирует значения перечисленных параметров, не трогая остальные.
"""

from __future__ import annotations

import logging
import re

SECRET_PARAMS = frozenset({"token", "api_key", "key", "code"})
_QUERY_RE = re.compile(r"\?(?P<query>[^\s\"]*)")


def redact_query(value: str) -> str:
    """Заменить значения секретных query-параметров на ``***``."""
    match = _QUERY_RE.search(value)
    if match is None:
        return value
    parts = match.group("query").split("&")
    redacted = []
    for part in parts:
        name, sep, _ = part.partition("=")
        redacted.append(f"{name}=***" if sep and name.lower() in SECRET_PARAMS else part)
    if redacted == parts:
        return value
    return f"{value[: match.start('query')]}{'&'.join(redacted)}{value[match.end('query') :]}"


class RedactSecretsFilter(logging.Filter):
    """Фильтр логгера: маскирует секреты в сообщении и в аргументах записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_query(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_query(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_query(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def install_access_log_redaction() -> None:
    """Поставить фильтр на access-лог uvicorn и на логгер приложения (идемпотентно)."""
    for name in ("uvicorn.access", "roma"):
        log = logging.getLogger(name)
        if not any(isinstance(f, RedactSecretsFilter) for f in log.filters):
            log.addFilter(RedactSecretsFilter())
