"""core/jev_shadow_log.py — JSONL-журнал shadow-наблюдений (для калибровки порогов).

Shadow-наблюдения Jev (R1/R3/R5) пишутся сюда отдельной строкой JSONL каждая,
чтобы после накопить размеченный датасет и пересчитать пороги (сейчас — заглушки).

Каждая строка — dict с ``point``, ``choice``, ``confidence``, ``verdict``
(детерминированный исход) и point-специфичными полями (``probabilities``,
``prob_anomaly``, ``zone``, ``heuristic_intent``). Запись — best-effort: сбой файла
не должен ломать горячий путь.

Путь: env ``JEV_SHADOW_LOG``, по умолчанию ``logs/jev_shadow.jsonl``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("core.jev_shadow_log")


def shadow_log_path() -> Path:
    return Path(os.getenv("JEV_SHADOW_LOG", "logs/jev_shadow.jsonl"))


def record(point: str, **fields: Any) -> None:
    """Дописать одну shadow-запись в JSONL (best-effort, не бросает наружу)."""
    try:
        path = shadow_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"point": point, **fields}, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("jev shadow log write failed: %r", exc)


def iter_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Итератор записей (для анализатора)."""
    p = path or shadow_log_path()
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
