"""Sidecar-адаптер Jev (TypeSafe System One) — эпоха G-JEV-BRIDGE, spike.

Ядро ROMA этот пакет НЕ импортирует: ни планировщик, ни шедулер, ни
decision-gate, ни биллинг, ни Stripe/CloudPayments, ни раннеры задач.
Точка входа внешняя — `python -m adapters.jev.run` (или обёртка процесса
снаружи). Контракт: `docs/spikes/G-JEV-BRIDGE.md`.

Что делает: превращает state задачи в один вызов
`POST https://api.typesafe.ai/v1/systemone` и возвращает
`execute` / `confirm` / `abort` / `skip`.

Чего не делает: не пишет план, не генерирует текст, не решает оплату, деплой,
удаление и квоты, и ничего не возвращает в ROMA — ядро его не зовёт.
"""

from adapters.jev.adapter import DECISIONS, SKIP_REASONS, JevAdapter, decide
from adapters.jev.client import JevClient, is_enabled
from adapters.jev.contract import DEFAULT_MODEL, ENDPOINT, LIMITS, THRESHOLDS

__all__ = [
    "DECISIONS",
    "DEFAULT_MODEL",
    "ENDPOINT",
    "LIMITS",
    "SKIP_REASONS",
    "THRESHOLDS",
    "JevAdapter",
    "JevClient",
    "decide",
    "is_enabled",
]
