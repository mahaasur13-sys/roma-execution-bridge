#!/usr/bin/env python3
"""N6/A4: единый drift-контроль «репозиторный канон ↔ то, что реально исполняется».

Зачем: операционный код живёт вне репозитория (tree B) и дрейфует молча — ревью видит
tracked-копию, а работает другая. Класс дефекта N6 зафиксирован в аудите 2026-09-21:
исполняемая копия сторожа содержала фикс R3, а версионированная — нет (расхождение 21 строка).
Класс A4: security-контроль (барьер Grafana) тоже живёт вне git и может быть молча отменён
регенерацией платформенного конфига.
Класс R6: платформенный promtail-конфиг /__substrate/logging/promtail-config.yaml регенерируется молча
и теряет джобу pg_watchdog_persistent — журнал сторожа /var/lib/pg-watchdog/watchdog.log перестаёт
доходить до Loki. Ловится фингерпринтом живого конфига + снапшотом в git + обязательной джобой с label.

Политика:
  * только чтение и сравнение; НИКАКИХ авто-перезаписей — repo→executed применяется вручную
    с GO, иначе отставший репозиторий молча откатит живой фикс;
  * расхождение/отсутствие → строка в лог + алерт через alert-relay + exit≠0;
  * --ci: отсутствие executed_path (чужая машина, CI-раннер) — предупреждение, а не провал;
  * проба пароля: только 127.0.0.1, пароль не логируется ни при каком исходе.

R6: платформенный promtail-конфиг (/__substrate/logging/promtail-config.yaml) регенерируется
платформой и молча теряет джобу pg_watchdog_persistent — строки сторожа перестают доходить
до Loki. Фикс жил вне git под ложным именем pre-r3fix. Проверка promtail_config сверяет:
живой sha256 == записанный фингерпринт == версионированный снапшот, и что обязательная джоба
(с точным label в Loki) присутствует. Регенерация ловится дрейф-проверкой, а не глазами.

Проверки A4 (fail-closed барьер Grafana), все — по манифесту:
  shim-exists · shim-drift (сравнение с каноном) · path-priority (command -v) ·
  conf-bypass (платформенный конфиг не должен звать бинарь напрямую) ·
  sealed-present (файл секрета есть и непуст) ·
  probe (admin/admin отвечает 200 → КРИТИЧНО: алерт через relay + остановка сервиса).

Проверки R6 (promtail_config), все — по манифесту:
  live-exists · fingerprint-drift (sha живого против deploy/monitoring/promtail-config.sha256) ·
  canon-drift (sha живого против снапшота в git) · job-missing · job-label-drift
  (подмена job: pg_watchdog на другое имя — поток в Loki был бы не тот).

Запуск:  python3 scripts/drift_check.py [--ci] [--no-probe] [--no-alert] [--manifest PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "deploy" / "ops" / "executed_paths.json"
RELAY_URL = "http://127.0.0.1:8099/notify"
STATE_PATH = Path("/home/workspace/artifacts/a4-grafana-drift/state.json")
ALERT_THROTTLE_S = 900


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def notify(
    title: str, status: str, rows: list[tuple[str, str, str]]
) -> tuple[bool, str]:
    alerts = [
        {
            "status": "firing",
            "labels": {"alertname": "DriftDetected"},
            "annotations": {"summary": f"{st}: {name} — {detail}"},
        }
        for name, st, detail in rows
    ]
    payload = {"title": title, "status": status, "alerts": alerts}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RELAY_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200, f"http {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def supervisorctl(conf: str, *argv: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["supervisorctl", "-c", conf, *argv],
            capture_output=True,
            text=True,
            timeout=25,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def program_command(conf: str, program: str) -> str | None:
    try:
        text = Path(conf).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    marker = f"[program:{program}]"
    start = text.find(marker)
    if start < 0:
        return None
    block: list[str] = []
    for line in text[start + len(marker) :].splitlines():
        if line.strip().startswith("[") and line.strip().endswith("]"):
            break
        if line.startswith("command="):
            block.append(line[len("command=") :])
        elif block and line.startswith((" ", "\t")):
            block.append(line.strip())
    return "\n".join(block) if block else None


def probe_default_password(url: str) -> tuple[bool, str]:
    """POST /api/login с admin/admin. Пароль не логируется; ответ — только код."""
    if not url.startswith("http://127.0.0.1:") and not url.startswith(
        "http://localhost:"
    ):
        return False, "refused: проба разрешена только с 127.0.0.1"
    body = json.dumps({"user": "admin", "password": "admin"}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200, f"http {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__} (сервис недоступен)"


def parse_sha_file(path: Path) -> str | None:
    """Записанный фингерпринт из файла sha256sum-формата (первый token)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            token = line.split()[0] if line.split() else ""
            if len(token) == 64 and all(c in "0123456789abcdef" for c in token.lower()):
                return token.lower()
    except OSError:
        return None
    return None


def job_block(text: str, job_name: str) -> str | None:
    """Вложенный блок одной scrape_configs-джобы — по отступу.

    Блочный разбор по «- » невозможен: внутри джобы есть вложенные списки
    (targets/labels), поэтому границы блока определяются уровнем job_name.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped not in (f"job_name: {job_name}", f"- job_name: {job_name}"):
            continue
        indent = len(line) - len(line.lstrip())
        block = [line]
        for nxt in lines[i + 1 :]:
            if not nxt.strip():
                block.append(nxt)
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                break
            block.append(nxt)
        return "\n".join(block)
    return None


def check_promtail_config(
    chk: dict, name: str, args: argparse.Namespace
) -> list[tuple[str, str, str]]:
    """R6: живой платформенный promtail-конфиг против фингерпринта, снапшота и обязательной джобы.

    Три независимых признака, потому что регенерация может сохранить один и потерять другой:
    sha живого == фингерпринт, sha живого == снапшот в git, обязательная джоба с нужным label.
    Ничего не перезаписываем: расхождение — только сигнал (платформа может регенерировать конфиг снова).
    """
    problems: list[tuple[str, str, str]] = []

    def fail(kind: str, detail: str) -> list[tuple[str, str, str]]:
        print(f"CHECK {name}: {kind} — {detail}")
        problems.append((name, kind, detail))
        return problems

    live = Path(chk["live_path"])
    canon = REPO_ROOT / chk["canon_path"]
    sha_file = REPO_ROOT / chk["sha256_file"]
    required_job = chk.get("required_job")
    required_label = chk.get("required_job_label")
    log_path = chk.get("log_path", "журнал сторожа")

    if not live.exists():
        if args.ci:
            print(
                f"CHECK {name}: SKIP живой платформенный конфиг недоступен (--ci): {live}"
            )
            return problems
        return fail("LIVE-MISSING", f"живой платформенный конфиг не найден: {live}")
    if not canon.exists():
        return fail(
            "CANON-MISSING",
            f"версионированный снапшот отсутствует: {chk['canon_path']}",
        )
    if not sha_file.exists():
        return fail(
            "FINGERPRINT-MISSING",
            f"файл фингерпринта отсутствует: {chk['sha256_file']}",
        )

    live_sha, canon_sha = sha256(live), sha256(canon)
    recorded = parse_sha_file(sha_file)

    if recorded is None:
        fail(
            "FINGERPRINT-UNPARSABLE",
            f"не прочитан записанный фингерпринт: {chk['sha256_file']}",
        )
    elif recorded != live_sha:
        fail(
            "FINGERPRINT-DRIFT",
            f"живой sha256={live_sha[:12]} != записанный {recorded[:12]}: платформенный конфиг "
            f"изменён/регенерирован — проверить, что джоба {required_job} на месте",
        )
    if live_sha != canon_sha:
        fail(
            "CANON-DRIFT",
            f"живой sha256={live_sha[:12]} != снапшот в git {canon_sha[:12]} ({chk['canon_path']})",
        )

    block = job_block(live.read_text(encoding="utf-8", errors="replace"), required_job)
    if block is None:
        fail(
            "JOB-MISSING",
            f"в живом конфиге нет джобы {required_job}: строки {log_path} не доходят до Loki (класс R6)",
        )
    elif required_label and not any(
        ln.strip() == f"job: {required_label}" for ln in block.splitlines()
    ):
        fail(
            "JOB-LABEL-DRIFT",
            f"джоба {required_job} есть, но label 'job: {required_label}' в ней отсутствует — "
            f"поток в Loki подменён",
        )
    elif "__path__" not in block:
        fail("JOB-PATH-MISSING", f"в джобе {required_job} нет __path__")

    if not problems:
        print(
            f"CHECK {name}: OK sha256={live_sha[:12]} джоба {required_job} "
            f"(label job: {required_label}) на месте, снапшот совпадает"
        )
    return problems


def run_pairs(
    pairs: list[dict], args: argparse.Namespace
) -> tuple[list[tuple[str, str, str]], int]:
    problems: list[tuple[str, str, str]] = []
    compared = 0
    for pair in pairs:
        name = pair.get("name", "?")
        repo_path = REPO_ROOT / pair["repo_path"]
        executed_path = Path(pair["executed_path"])

        if not repo_path.exists():
            problems.append(
                (name, "REPO-MISSING", f"канон отсутствует: {pair['repo_path']}")
            )
            print(f"DRIFT {name}: REPO-MISSING {pair['repo_path']}")
            continue
        if not executed_path.exists():
            kind = "SKIP" if args.ci else "EXEC-MISSING"
            print(f"DRIFT {name}: {kind} executed_path не найден: {executed_path}")
            if not args.ci:
                problems.append(
                    (
                        name,
                        "EXEC-MISSING",
                        f"исполняемый файл не найден: {executed_path}",
                    )
                )
            continue

        repo_sha, exec_sha = sha256(repo_path), sha256(executed_path)
        compared += 1
        if repo_sha == exec_sha:
            print(f"DRIFT {name}: OK sha256={repo_sha[:12]}")
        else:
            print(
                f"DRIFT {name}: MISMATCH repo={repo_sha[:12]} executed={exec_sha[:12]}"
            )
            problems.append(
                (
                    name,
                    "MISMATCH",
                    f"repo={repo_sha[:12]} executed={exec_sha[:12]} ({pair['repo_path']} vs {executed_path})",
                )
            )
    return problems, compared


def run_checks(
    checks: list[dict], args: argparse.Namespace
) -> list[tuple[str, str, str]]:
    problems: list[tuple[str, str, str]] = []
    checked = 0
    state = load_state()

    for chk in checks:
        kind = chk.get("type")
        name = chk.get("name", kind or "?")
        if kind == "promtail_config":
            checked += 1
            problems += check_promtail_config(chk, name, args)
            continue
        if kind != "grafana_fail_closed":
            problems.append(
                (name, "UNKNOWN-CHECK", f"неизвестный тип проверки: {kind!r}")
            )
            continue
        checked += 1
        shim = Path(chk["shim"])
        canon = REPO_ROOT / chk["shim_canon"]
        sealed = Path(chk["sealed"])
        conf = chk["supervisor_conf"]
        program = chk["program"]

        if not shim.exists():
            problems.append((name, "SHIM-MISSING", f"барьер отсутствует: {shim}"))
        else:
            if not os.access(shim, os.X_OK):
                problems.append((name, "SHIM-NOT-EXECUTABLE", f"{shim} не исполняем"))
            if not canon.exists():
                problems.append(
                    (
                        name,
                        "SHIM-CANON-MISSING",
                        f"канон барьера отсутствует: {chk['shim_canon']}",
                    )
                )
            elif sha256(shim) != sha256(canon):
                problems.append(
                    (
                        name,
                        "SHIM-DRIFT",
                        f"sha256 {sha256(shim)[:12]} != канон {sha256(canon)[:12]} ({chk['shim_canon']})",
                    )
                )

        resolved = shutil.which("grafana-server")
        if resolved != str(shim):
            problems.append(
                (
                    name,
                    "PATH-DRIFT",
                    f"command -v grafana-server = {resolved or '<не найден>'} (ожидался {shim}): "
                    "приоритет PATH потерян, барьер не перехватит запуск",
                )
            )

        command = program_command(conf, program)
        if command is None:
            problems.append(
                (
                    name,
                    "CONF-PROGRAM-MISSING",
                    f"в {conf} нет секции [program:{program}]",
                )
            )
        elif "/usr/sbin/grafana-server" in command:
            problems.append(
                (
                    name,
                    "CONF-BYPASS",
                    f"[program:{program}] зовёт бинарь напрямую — барьер обойдён",
                )
            )
        elif "grafana-server" not in command:
            problems.append(
                (
                    name,
                    "CONF-NO-GRAFANA",
                    f"[program:{program}] не запускает grafana-server",
                )
            )

        if not sealed.exists() or sealed.stat().st_size == 0:
            problems.append(
                (name, "SEALED-MISSING", f"sealed-файл отсутствует или пуст: {sealed}")
            )

        platform_sealed = chk.get("platform_sealed")
        if platform_sealed:
            ps = Path(platform_sealed)
            if not ps.exists() or ps.stat().st_size == 0:
                problems.append(
                    (
                        name,
                        "PLATFORM-SEALED-MISSING",
                        f"платформенный путь {platform_sealed} (его читает command супервизора) отсутствует: "
                        f"Grafana не стартует. Восстановление: ln -sfn {sealed} {platform_sealed}",
                    )
                )

        if args.no_probe:
            print(f"CHECK {name}: probe отключена (--no-probe)")
        else:
            weak, detail = probe_default_password(chk["probe_url"])
            if weak:
                problems.append(
                    (
                        name,
                        "FAIL-OPEN",
                        f"admin/admin принят ({detail}) — Grafana стартовала с дефолтным паролем",
                    )
                )
                print(f"CHECK {name}: КРИТИЧНО admin/admin принят ({detail})")
                if not args.no_stop:
                    auto_stop(name, conf, program, state)
            else:
                print(
                    f"CHECK {name}: probe admin/admin отклонён ({detail}) — барьер работает"
                )
                cstate = state.setdefault("checks", {}).setdefault(name, {})
                cstate.pop("stopped_at", None)
                cstate["last_probe_ok_at"] = int(time.time())

    save_state(state)
    if not problems:
        print(f"CHECKS: ok ({checked} проверок)")
    return problems


def auto_stop(name: str, conf: str, program: str, state: dict) -> None:
    """Идемпотентная остановка сервиса: повторный запуск не даёт ошибок и не спамит."""
    cstate = state.setdefault("checks", {}).setdefault(name, {})
    code, status = supervisorctl(conf, "status", program)
    already_stopped = "STOPPED" in status or "not running" in status
    if already_stopped:
        print(
            f"CHECK {name}: сервис уже остановлен ({status.splitlines()[0][:80]}) — повторная остановка не нужна"
        )
        cstate.setdefault("stopped_at", int(time.time()))
        return

    stop_code, stop_out = supervisorctl(conf, "stop", program)
    cstate["stopped_at"] = int(time.time())
    print(
        f"CHECK {name}: авто-стоп {program} → rc={stop_code} {stop_out.splitlines()[-1][:80] if stop_out else ''}"
    )

    last_alert = int(cstate.get("last_alert_at") or 0)
    if time.time() - last_alert < ALERT_THROTTLE_S:
        print(
            f"CHECK {name}: алерт подавлен троттлингом ({int(time.time() - last_alert)}s с прошлого)"
        )
        return
    ok, info = notify(
        "ROMA A4: Grafana fail-open",
        "firing",
        [
            (
                name,
                "FAIL-OPEN",
                f"admin/admin принят на {program}; сервис остановлен автоматически",
            )
        ],
    )
    cstate["last_alert_at"] = int(time.time())
    print(f"CHECK {name}: alert_relay={'sent' if ok else 'FAILED'} ({info})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument(
        "--ci",
        action="store_true",
        help="отсутствие executed_path — warning, не провал",
    )
    ap.add_argument(
        "--no-alert", action="store_true", help="не слать алерт (dry-run/CI)"
    )
    ap.add_argument("--no-probe", action="store_true", help="не пробовать admin/admin")
    ap.add_argument(
        "--no-stop", action="store_true", help="не останавливать сервис при fail-open"
    )
    ap.add_argument(
        "--checks",
        choices=["auto", "only", "skip"],
        default="auto",
        help="auto: проверки A4 выполняются, если среда на месте",
    )
    args = ap.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    pairs = manifest.get("pairs", [])
    checks = manifest.get("checks", [])

    problems, compared = run_pairs(pairs, args)
    if args.checks != "skip":
        problems += run_checks(checks, args)
    print(
        f"drift-check: pairs={len(pairs)} compared={compared} checks={len(checks)} problems={len(problems)}"
    )

    if problems and not args.no_alert:
        state = load_state()
        agg = state.setdefault("aggregate", {})
        signature = ";".join(sorted(f"{n}:{k}" for n, k, _ in problems))
        last_alert = int(agg.get("last_alert_at") or 0)
        if (
            agg.get("signature") == signature
            and time.time() - last_alert < ALERT_THROTTLE_S
        ):
            print(
                f"drift-check: сводный алерт подавлен троттлингом "
                f"({int(time.time() - last_alert)}s, набор проблем не изменился)"
            )
        else:
            ok, info = notify(
                "ROMA drift-check: канон != исполняемое", "firing", problems
            )
            agg["signature"] = signature
            agg["last_alert_at"] = int(time.time())
            print(f"drift-check: alert_relay={'sent' if ok else 'FAILED'} ({info})")
        save_state(state)

    if problems:
        print("DRIFT-CHECK: FAILED")
        return 1
    print("DRIFT-CHECK: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
