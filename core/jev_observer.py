"""core/jev_observer.py — общий bounded-планировщик фоновых shadow-наблюдений.

Один ленивый ``ThreadPoolExecutor`` на процесс; бюджет «выполняется + в очереди»
ограничен ``BoundedSemaphore``, который захватывается БЕЗ блокировки. Нет места —
наблюдение пропускается (предупреждение в логе), вердикт никогда не блокируется
и не меняется.

``submit_observation`` НИКОГДА не бросает наружу: любая ошибка (в т.ч. ``RuntimeError``
после shutdown) → ``False``. Executor создаётся лениво — при выключенных shadow-флагах
он вообще не создаётся (нет накладных расходов).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from monitoring.metrics import track_jev_shadow

logger = logging.getLogger("core.jev_observer")

_MAX_WORKERS = 2
_QUEUE_BUDGET = 4  # суммарно «выполняется + ждёт в очереди»
_OVERFLOW_LOG_INTERVAL_S = 5.0

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_slots = threading.BoundedSemaphore(_QUEUE_BUDGET)

_dropped_total = 0
_dropped_since_log = 0
_last_overflow_log = 0.0
_overflow_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="jev-shadow")
    return _executor


def _count_drop(point: str) -> None:
    """Метрика дропа — best-effort: телеметрия не имеет права влиять на submit."""
    try:
        track_jev_shadow(point, dropped=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("jev shadow drop metric failed: %r", exc)


def _note_overflow(point: str = "unknown") -> None:
    """Считать пропуск; логировать не чаще раза в ``_OVERFLOW_LOG_INTERVAL_S``."""
    global _dropped_total, _dropped_since_log, _last_overflow_log
    _count_drop(point)
    with _overflow_lock:
        _dropped_total += 1
        _dropped_since_log += 1
        now = time.monotonic()
        if now - _last_overflow_log >= _OVERFLOW_LOG_INTERVAL_S:
            logger.warning(
                "jev shadow observer: queue full — %d dropped since last log (%d total)",
                _dropped_since_log,
                _dropped_total,
            )
            _dropped_since_log = 0
            _last_overflow_log = now


def submit_observation(fn: Callable[..., Any], *args: Any, point: str = "unknown") -> bool:
    """Поставить ``fn(*args)`` в очередь. ``False``, если нет места или submit упал.

    ``point`` — метка shadow-точки для метрик дропов (``R1``/``R3``/``R5``).
    Не бросает исключений: вызывающий (shadow-harness) всегда продолжает с вердиктом.
    """
    if not _slots.acquire(blocking=False):
        _note_overflow(point)
        return False
    try:
        future = _get_executor().submit(fn, *args)
    except Exception as exc:  # noqa: BLE001 — в т.ч. RuntimeError после shutdown
        _slots.release()
        _count_drop(point)
        logger.warning("jev shadow observer: submit failed (%r), observation dropped", exc)
        return False

    def _release(_f: Any) -> None:
        _slots.release()

    future.add_done_callback(_release)
    return True
