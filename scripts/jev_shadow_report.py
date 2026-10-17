#!/usr/bin/env python3
"""Минимальный анализатор shadow-логов Jev (подготовка к калибровке порогов).

Читает JSONL из ``core.jev_shadow_log`` (env ``JEV_SHADOW_LOG``, по умолчанию
``logs/jev_shadow.jsonl``) и печатает по каждой точке (R1/R3/R5): количество
записей, распределение ``choice``, распределение детерминированного ``verdict``
и сводку confidence (min/mean/max + грубые корзины).

Использование:
    python scripts/jev_shadow_report.py [путь-к-jsonl]
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.jev_shadow_log import iter_records  # noqa: E402

_BUCKETS = (
    (0.95, "0.95-1.00"),
    (0.90, "0.90-0.95"),
    (0.85, "0.85-0.90"),
    (0.70, "0.70-0.85"),
    (0.65, "0.65-0.70"),
    (0.0, "0.00-0.65"),
)


def _bucket(conf: float) -> str:
    for floor, label in _BUCKETS:
        if conf >= floor:
            return label
    return "?"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else None
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in iter_records(path):
        groups[rec.get("point", "?")].append(rec)

    if not groups:
        print("Нет записей (пусто). Сначала включите shadow-флаги и соберите данные.")
        return 0

    for point in sorted(groups):
        recs = groups[point]
        confs = [r["confidence"] for r in recs if r.get("confidence") is not None]
        print(f"\n== {point}: n={len(recs)} ==")
        print("  choice:   ", dict(Counter(str(r.get("choice", "?")) for r in recs)))
        print("  verdict:  ", dict(Counter(str(r.get("verdict", "?")) for r in recs)))
        if confs:
            print(
                f"  confidence: min={min(confs):.3f} mean={sum(confs) / len(confs):.3f} max={max(confs):.3f}"
            )
            print("  buckets:  ", dict(Counter(_bucket(c) for c in confs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
