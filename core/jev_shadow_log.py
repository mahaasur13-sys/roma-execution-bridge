"""core/jev_shadow_log.py — JSONL-журнал shadow-наблюдений (для калибровки порогов).

Shadow-наблюдения Jev (R1/R3/R5) пишутся сюда отдельной строкой JSONL каждая,
чтобы после накопить размеченный датасет и пересчитать пороги (сейчас — заглушки).

Каждая строка — dict с ``point``, ``choice``, ``confidence``, ``verdict``
(детерминированный исход) и point-специфичными полями (``probabilities``,
``prob_anomaly``, ``zone``, ``heuristic_intent``). Запись — best-effort: сбой файла
не должен ломать горячий путь.

Путь: env ``JEV_SHADOW_LOG``, по умолчанию ``logs/jev_shadow.jsonl``.

Ротация по размеру: ``JEV_SHADOW_LOG_MAX_BYTES`` (дефолт 50 MB), ``JEV_SHADOW_LOG_BACKUPS``
(дефолт 5) → ``<log>.1`` … ``<log>.N``, старейший бэкап удаляется. Запись и ротация —
под одним lock (пишут два воркера observer'а); сбой ротации не теряет запись.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("core.jev_shadow_log")

_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_BACKUPS = 5
_ROTATE_WARN_INTERVAL_S = 60.0

_LOCK = threading.Lock()
_rotate_warn_ts = 0.0


def shadow_log_path() -> Path:
    return Path(os.getenv("JEV_SHADOW_LOG", "logs/jev_shadow.jsonl"))


def _env_int(name: str, default: int) -> int:
    """Нечисловое или неположительное значение env → дефолт."""
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _max_bytes() -> int:
    return _env_int("JEV_SHADOW_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES)


def _backups() -> int:
    return _env_int("JEV_SHADOW_LOG_BACKUPS", _DEFAULT_BACKUPS)


def _warn_rotate(exc: OSError) -> None:
    """Не чаще раза в 60 с — иначе битая ротация заливает лог."""
    global _rotate_warn_ts
    now = time.monotonic()
    if now - _rotate_warn_ts < _ROTATE_WARN_INTERVAL_S:
        return
    _rotate_warn_ts = now
    logger.warning("jev shadow log rotation failed (append continues): %r", exc)


def _rotate(path: Path) -> None:
    """``<log>`` → ``<log>.1``, ``<log>.i`` → ``<log>.i+1``, старейший удаляется."""
    backups = _backups()
    oldest = path.with_name(f"{path.name}.{backups}")
    if oldest.exists():
        oldest.unlink()
    for i in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def _rotate_if_needed(path: Path, incoming: int) -> None:
    """Ротация до записи, если запись вывела бы файл за лимит.

    Сбой ротации проглатывается: запись всё равно должна попасть в файл.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size + incoming <= _max_bytes():
        return
    try:
        _rotate(path)
    except OSError as exc:
        _warn_rotate(exc)


def record(point: str, **fields: Any) -> None:
    """Дописать одну shadow-запись в JSONL (best-effort, не бросает наружу)."""
    try:
        path = shadow_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"point": point, **fields}, ensure_ascii=False, sort_keys=True) + "\n"
        with _LOCK:
            _rotate_if_needed(path, len(line.encode("utf-8")))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as exc:
        logger.warning("jev shadow log write failed: %r", exc)


def iter_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Итератор записей (для анализатора).

    shortcut: читает только живой файл, не бэкапы `.1..N` — при включённой ротации
    старые записи в анализ не попадают; расширять при разборе >50 MB истории.
    """
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
