#!/usr/bin/env python3
"""N6: единый drift-check «репозиторный канон ↔ исполняемый файл».

Зачем: операционный код живёт вне репозитория (tree B) и дрейфует молча — ревью видит
tracked-копию, а работает другая. Класс дефекта N6 зафиксирован в аудите 2026-09-21:
исполняемая копия сторожа содержала фикс R3, а версионированная — нет (расхождение 21 строка).

Политика:
  * сравнение только ЧТЕНИЕМ (sha256), никаких авто-перезаписей — repo→executed применяется
    вручную с GO, иначе отставший репозиторий молча откатит живой фикс;
  * расхождение/отсутствие → строка в лог + алерт через alert-relay + exit≠0;
  * --ci: отсутствие executed_path (чужая машина, CI-раннер) — предупреждение, а не провал.

Запуск:  python3 scripts/drift_check.py [--ci] [--manifest deploy/ops/executed_paths.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "deploy" / "ops" / "executed_paths.json"
RELAY_URL = "http://127.0.0.1:8099/notify"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def notify(title: str, status: str, rows: list[tuple[str, str]]) -> tuple[bool, str]:
    alerts = [{"status": "firing", "labels": {"alertname": "DriftDetected"},
               "annotations": {"summary": f"{st}: {name} — {detail}"}} for name, st, detail in rows]
    payload = {"title": title, "status": status, "alerts": alerts}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(RELAY_URL, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200, f"http {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--ci", action="store_true", help="отсутствие executed_path — warning, не провал")
    ap.add_argument("--no-alert", action="store_true", help="не слать алерт (dry-run/CI)")
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    problems: list[tuple[str, str, str]] = []
    checked = 0

    for pair in manifest.get("pairs", []):
        name = pair.get("name", "?")
        repo_path = REPO_ROOT / pair["repo_path"]
        executed_path = Path(pair["executed_path"])

        if not repo_path.exists():
            problems.append((name, "REPO-MISSING", f"канон отсутствует: {pair['repo_path']}"))
            print(f"DRIFT {name}: REPO-MISSING {pair['repo_path']}")
            continue
        if not executed_path.exists():
            kind = "SKIP" if args.ci else "EXEC-MISSING"
            print(f"DRIFT {name}: {kind} executed_path не найден: {executed_path}")
            if not args.ci:
                problems.append((name, "EXEC-MISSING", f"исполняемый файл не найден: {executed_path}"))
            continue

        repo_sha, exec_sha = sha256(repo_path), sha256(executed_path)
        checked += 1
        if repo_sha == exec_sha:
            print(f"DRIFT {name}: OK sha256={repo_sha[:12]}")
        else:
            print(f"DRIFT {name}: MISMATCH repo={repo_sha[:12]} executed={exec_sha[:12]}")
            problems.append((name, "MISMATCH",
                             f"repo={repo_sha[:12]} executed={exec_sha[:12]} ({pair['repo_path']} vs {executed_path})"))

    print(f"drift-check: pairs={len(manifest.get('pairs', []))} compared={checked} problems={len(problems)}")

    if problems and not args.no_alert:
        ok, info = notify("ROMA drift-check: канон != исполняемое", "firing", problems)
        print(f"drift-check: alert_relay={'sent' if ok else 'FAILED'} ({info})")

    if problems:
        print("DRIFT-CHECK: FAILED")
        return 1
    print("DRIFT-CHECK: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
