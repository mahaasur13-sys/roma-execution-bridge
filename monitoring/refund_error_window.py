"""F02-A: скользящее окно ошибок list_error refund_pending-свипера (в приложении).

Требование F02 (не меняется): не менее трёх ошибок list_error в скользящем окне
15 минут, условие непрерывно выполняется пять минут (``for: 5m`` в правиле).

Решения владельца 2026-11-09 (OWNER DECISION RECORDED):

* O1 — событие входит в окно, если ``event_time ∈ (now − 900s, now]``. Событие
  ровно на ``now − 900s`` НЕ входит. Событие из «будущего» (``event_time > now``,
  например при скачке часов) в текущий счёт не входит, но и не выбрасывается.
* O2 — суммирование по процессам делает правило Prometheus (``sum(...)``).
  Здесь — счёт одного процесса. OPEN: ключ события и дедупликация между
  процессами (кто владелец ключа) — не решены, ключ не придумывается.
* O3 — окно считает приложение; наружу — gauge ``roma_refund_pending_errors_15m``.
* O4 — история окна живёт в памяти процесса и теряется при рестарте. Окно
  достоверно (``complete``) только если с момента старта окна прошло ≥ 900s;
  до этого счёт — нижняя граница, а НЕ «ноль ошибок». OPEN: модель
  восстановления истории после рестарта.
* F02-D (``x - x offset``) и F02-E/``received_at`` — не используются.

OPEN: ``event_time`` берётся из ``time.time()`` (wall clock) процесса в момент
фиксации ошибки; поведение при скачках часов (NTP) не специфицировано владельцем.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

WINDOW_SECONDS = 900.0


class ErrorWindow:
    """Потокобезопасный счётчик событий в полуоткрытом окне (now − W, now]."""

    def __init__(self, window_seconds: float = WINDOW_SECONDS, clock: Callable[[], float] = time.time):
        self._window = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._events: deque[float] = deque()
        self._started_at = float(clock())

    @property
    def window_seconds(self) -> float:
        return self._window

    @property
    def started_at(self) -> float:
        """Epoch seconds старта окна (= старт процесса/импорта модуля, O4)."""
        return self._started_at

    def record(self, event_time: float | None = None) -> None:
        t = float(self._clock() if event_time is None else event_time)
        with self._lock:
            self._events.append(t)
            self._prune(float(self._clock()))

    def _prune(self, now: float) -> None:
        lo = now - self._window
        # Удаляем только то, что уже никогда не войдёт в окно: t <= now − W.
        # deque не обязана быть отсортирована (event_time может прийти не по
        # порядку) — поэтому пересобираем, а не popleft до первого «свежего».
        if any(t <= lo for t in self._events):
            self._events = deque(t for t in self._events if t > lo)

    def count(self, now: float | None = None) -> int:
        n = float(self._clock() if now is None else now)
        lo = n - self._window
        with self._lock:
            self._prune(n)
            return sum(1 for t in self._events if lo < t <= n)

    def complete(self, now: float | None = None) -> bool:
        """True, только если окно целиком покрыто временем жизни процесса (O4)."""
        n = float(self._clock() if now is None else now)
        return (n - self._started_at) >= self._window

    def reset(self, started_at: float | None = None) -> None:
        """Только для тестов: имитация рестарта процесса (история теряется)."""
        with self._lock:
            self._events.clear()
            self._started_at = float(self._clock() if started_at is None else started_at)


# Окно list_error refund_pending-свипера, один экземпляр на процесс.
refund_list_error_window = ErrorWindow()
