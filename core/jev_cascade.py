"""Fail-closed обёртка для каскадных вызовов Jev.

Любая ошибка (сеть, таймаут, ``JevError`` с ``reason="model_pin"`` / ``"model_pin_response"``,
неожиданный ответ, исключение внутри самого каскада) → ``baseline``, без исключения наружу.
``asyncio.CancelledError`` не глотается никогда.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger("roma.jev.cascade")


def safe_cascade(baseline: Any, fn: Callable[..., Any], *args: Any, label: str, **kwargs: Any) -> Any:
    """``fn(*args, **kwargs)``, а при любой ошибке — ``baseline``.

    В лог попадают только ``label`` и тип исключения: ни данных запроса, ни текста ошибки.
    """
    try:
        return fn(*args, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail-closed по контракту
        logger.warning("jev cascade %s failed (%s) — baseline kept", label, type(exc).__name__)
        return baseline
