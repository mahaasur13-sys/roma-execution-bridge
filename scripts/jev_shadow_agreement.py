#!/usr/bin/env python3
"""Анализ agreement Jev ↔ детерминированный вердикт поверх jev_shadow.jsonl.

Считает по каждой точке (R1/R3/R5):
    * agreement rate (совпадает ли класс Jev с детерминированным исходом);
    * flip-rate (1 - agreement);
    * направление расхождения (Jev «строже» / «мягче»);
    * распределение confidence (min/mean/max + корзины).

ПРЕДПОЛОЖЕНИЕ (зафиксировано явно): словари Jev и детерминированного вердикта разные,
поэтому agreement считается через «compliant / non-compliant» отображение:

    R1: choice normal==compliant, anomaly==non-compliant;
        verdict allowed==compliant, иначе non-compliant.
    R3: choice compliant==compliant, violation/escalate==non-compliant;
        verdict allow==compliant, иначе non-compliant.
    R5: словари совпадают (choice == verdict/heuristic) → прямое равенство строк.

Отображение настраивается в COMPLIANT; пересмотреть при появлении реальных данных.

Использование:
    python scripts/jev_shadow_agreement.py [путь-к-jsonl]
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.jev_shadow_log import iter_records  # noqa: E402

# Отображение «compliant» (начальное предположение).
COMPLIANT: dict[str, dict[str, set[str]]] = {
    "R1": {"choice": {"normal"}, "verdict": {"allowed"}},
    "R3": {"choice": {"compliant"}, "verdict": {"allowed"}},
    # R5 отсутствует → agreement = прямое равенство choice == verdict
}

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


def _classify(point: str, choice: str, verdict: str) -> str | None:
    """'agree' | 'jev_stricter' | 'jev_laxer' | 'jev_differs' | None (не считаем)."""
    if point == "R5":
        if not choice or not verdict:
            return None
        return "agree" if choice == verdict else "jev_differs"
    cfg = COMPLIANT.get(point)
    if not cfg or not choice or not verdict:
        return None
    jc = choice in cfg["choice"]
    dc = verdict in cfg["verdict"]
    if jc == dc:
        return "agree"
    return "jev_stricter" if (not jc and dc) else "jev_laxer"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else None
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in iter_records(path):
        groups[rec.get("point", "?")].append(rec)

    if not groups:
        print("Нет записей. Сначала включите shadow-флаги и накопите JEV_SHADOW_LOG.")
        return 0

    for point in sorted(groups):
        recs = groups[point]
        classes: Counter[str] = Counter()
        for rec in recs:
            cls = _classify(point, str(rec.get("choice") or ""), str(rec.get("verdict") or ""))
            if cls is not None:
                classes[cls] += 1

        total = sum(classes.values())
        agree = classes.get("agree", 0)
        stricter = classes.get("jev_stricter", 0)
        laxer = classes.get("jev_laxer", 0) + classes.get("jev_differs", 0)
        flip = total - agree

        confs = [r["confidence"] for r in recs if r.get("confidence") is not None]
        print(f"\n== {point}: n={len(recs)} ==")
        if total:
            print(f"  agreement: {agree}/{total} ({agree / total * 100:.1f}%)")
            print(f"  flip-rate: {flip}/{total} ({flip / total * 100:.1f}%)")
            if point == "R5":
                print(f"    - Jev != эвристика: {laxer}")
            else:
                print(f"    - Jev stricter (флагует compliant): {stricter}")
                print(f"    - Jev laxer   (пропускает violation): {laxer}")
        else:
            print("  (нет строк с choice+verdict для agreement)")
        if confs:
            print(f"  confidence: min={min(confs):.3f} mean={sum(confs) / len(confs):.3f} max={max(confs):.3f}")
            print("  buckets:   ", dict(Counter(_bucket(c) for c in confs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
