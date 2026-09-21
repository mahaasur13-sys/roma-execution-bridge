#!/usr/bin/env python3
"""PG watchdog — supervision for PostgreSQL cluster 15/main (127.0.0.1:5432).

Runs as a Zo user service (mode=process). Every INTERVAL seconds:
  1. probes readiness with pg_isready
  2. if down and restart budget allows -> pg_ctlcluster 15 main start, then waits for ready
  3. verifies append-only ledger row count never decreases (integrity tripwire)
  4. emits one JSON line per event to /dev/shm/pg-watchdog.log (collected by Promtail -> Loki)
     and keeps last state in /dev/shm/pg-watchdog-state.json

No DDL/DML is ever executed against the database: the only SQL is a read-only
SELECT count(*) FROM ledger_entries.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time

PORT = 5432
CLUSTER = "15"
INTERVAL = 30
RESTART_BUDGET_PER_HOUR = 5
READY_TIMEOUT_S = 60
# R3: state и журнал — на persistent-пути (/dev/shm теряется при перезапуске
# песочницы). История рестартов обязана переживать рестарт.
STATE_DIR = "/var/lib/pg-watchdog"
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOG_PATH = os.path.join(STATE_DIR, "watchdog.log")  # журнал событий, с ротацией
LOG_MAX_BYTES = 1_000_000  # 1 MB → watchdog.log.1
PSQL = [
    "/usr/sbin/runuser",
    "-u",
    "postgres",
    "--",
    "/usr/bin/psql",
    "-h",
    "/var/run/postgresql",
    "-p",
    str(PORT),
    "-d",
    "roma",
    "-tAc",
]


def now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def emit(**fields) -> None:
    line = json.dumps({"ts": now_iso(), **fields}, ensure_ascii=False)
    print(line, flush=True)  # stdout → супервизор → Loki (/dev/shm/pg-watchdog.log)
    try:  # R3: durable-копия на persistent-пути, переживает рестарт песочницы
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) >= LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # журнал не должен ронять супервизию
        print(
            json.dumps(
                {
                    "ts": now_iso(),
                    "event": "log_write_failed",
                    "error": type(exc).__name__,
                }
            ),
            flush=True,
        )


def pg_is_ready() -> bool:
    try:
        return (
            subprocess.run(
                ["/usr/bin/pg_isready", "-h", "127.0.0.1", "-p", str(PORT), "-q"],
                capture_output=True,
                timeout=15,
            ).returncode
            == 0
        )
    except Exception:
        return False


def read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def prune_restarts(restarts: list[str]) -> list[str]:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    keep = []
    for ts in restarts:
        try:
            if datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                keep.append(ts)
        except Exception:
            continue
    return keep


def ledger_rows() -> int | None:
    try:
        out = subprocess.run(
            PSQL + ["SELECT count(*) FROM ledger_entries"],
            capture_output=True,
            timeout=20,
        )
        if out.returncode != 0:
            return None
        return int(out.stdout.decode().strip())
    except Exception:
        return None


def start_cluster() -> bool:
    try:
        res = subprocess.run(
            ["/usr/bin/pg_ctlcluster", CLUSTER, "main", "start"],
            capture_output=True,
            timeout=120,
        )
        return res.returncode == 0
    except Exception:
        return False


def wait_ready(timeout_s: int) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pg_is_ready():
            return round(time.monotonic() - t0, 2)
        time.sleep(2)
    return None


def main() -> int:
    state = read_state()
    state.setdefault("restarts", [])
    state.setdefault("restarts_total", 0)
    if "ledger_baseline" not in state:
        state["ledger_baseline"] = ledger_rows()
    emit(
        event="watchdog_start",
        pid=os.getpid(),
        port=PORT,
        interval_s=INTERVAL,
        budget_per_hour=RESTART_BUDGET_PER_HOUR,
        ledger_baseline=state["ledger_baseline"],
    )

    while True:
        state["restarts"] = prune_restarts(state.get("restarts", []))
        up = pg_is_ready()
        state["pg_up"] = int(up)
        state["last_check"] = now_iso()
        state["restarts_last_hour"] = len(state["restarts"])

        if up:
            if state.get("was_down"):
                state["was_down"] = False
                emit(
                    event="pg_recovered",
                    pg_up=1,
                    restarts_last_hour=len(state["restarts"]),
                )
        else:
            state["was_down"] = True
            if len(state["restarts"]) >= RESTART_BUDGET_PER_HOUR:
                state["budget_exceeded"] = True
                emit(
                    event="pg_down_budget_exceeded",
                    pg_up=0,
                    restarts_last_hour=len(state["restarts"]),
                    action="none",
                )
            else:
                emit(event="pg_down", pg_up=0, action="pg_ctlcluster_start")
                t0 = time.monotonic()
                started = start_cluster()
                recovery = wait_ready(READY_TIMEOUT_S)
                if recovery is not None:
                    state["restarts"].append(now_iso())
                    state["restarts_total"] = state.get("restarts_total", 0) + 1
                    state["last_restart_at"] = now_iso()
                    state["last_recovery_s"] = recovery
                    state["budget_exceeded"] = False
                    emit(
                        event="pg_restart_ok",
                        pg_up=1,
                        started=started,
                        recovery_s=round(time.monotonic() - t0, 2),
                        restarts_total=state["restarts_total"],
                        restarts_last_hour=len(state["restarts"]),
                    )
                else:
                    emit(
                        event="pg_restart_failed",
                        pg_up=0,
                        started=started,
                        timeout_s=READY_TIMEOUT_S,
                        action="pg_ctlcluster_start",
                        recovery_s=None,
                    )

        rows = ledger_rows()
        if rows is not None:
            state["ledger_rows"] = rows
            baseline = state.get("ledger_baseline")
            if baseline is None:
                state["ledger_baseline"] = rows
            elif rows < baseline:
                emit(
                    event="ledger_integrity_violation",
                    pg_up=int(state["pg_up"]),
                    ledger_rows=rows,
                    ledger_baseline=baseline,
                )
            else:
                state["ledger_baseline"] = max(baseline, rows)

        write_state(state)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
