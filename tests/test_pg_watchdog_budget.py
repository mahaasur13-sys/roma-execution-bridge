"""R5: бюджет рестартов PG — поведенческое доказательство ветки budget_exceeded.

Канон: тест грузит КАНОНИЧЕСКИЙ файл `deploy/ops/pg_watchdog.py` (путь из репозитория),
а не исполняемую копию из tree B — иначе доказательство относится к непроверяемому артефакту.

Живой кластер не затрагивается: pg_is_ready/start_cluster/ledger_rows подменяются,
PostgreSQL не вызывается вообще (start_cluster вызовов = 0 при исчерпанном бюджете).
Состояние — во временном каталоге pytest (tmp_path), порядок прогона не влияет.

Выбор R5 (behavioral-тест вместо live-прогона): контролируемый live-прогон требует остановки
боевого кластера при append-only ledger — риск недоступности и загрязнения данных при
детерминированной бюджетной логике.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json

import pathlib

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CANON = REPO_ROOT / "deploy" / "ops" / "pg_watchdog.py"


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("pg_watchdog_under_test", CANON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_cycles(wd, tmp_path, *, recent_minutes: tuple[int, ...]) -> dict:
    state_path = tmp_path / "state.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = [
        (now - datetime.timedelta(minutes=m))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
        for m in recent_minutes
    ]
    state_path.write_text(
        json.dumps(
            {
                "restarts": recent,
                "restarts_total": 5,
                "ledger_baseline": 309,
                "was_down": False,
            }
        ),
        encoding="utf-8",
    )

    calls: list[str] = []
    wd.STATE_DIR = str(tmp_path)
    wd.STATE_PATH = str(state_path)
    wd.LOG_PATH = str(tmp_path / "watchdog.log")
    wd.pg_is_ready = lambda: False

    def fake_start_cluster() -> bool:
        calls.append("start_cluster")
        return True

    wd.start_cluster = fake_start_cluster
    wd.ledger_rows = lambda: None

    cycles = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        cycles["n"] += 1
        if cycles["n"] >= 2:
            raise SystemExit(0)

    wd.time.sleep = fake_sleep

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(SystemExit):
            wd.main()

    events = [
        json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")
    ]
    log_path = tmp_path / "watchdog.log"
    return {
        "events": events,
        "state": json.loads(state_path.read_text(encoding="utf-8")),
        "calls": calls,
        "log": log_path.read_text(encoding="utf-8") if log_path.exists() else "",
        "cycles": cycles["n"],
    }


def test_restart_budget_gate(tmp_path) -> None:
    """Бюджет исчерпан → кластер НЕ поднимается; бюджет свободен → поднимается.

    Обе половины в одном тесте намеренно: без второй (positive control) первая
    прошла бы и на «стороже, который никогда не рестартует».
    """
    wd = _load_watchdog()

    exhausted_dir = tmp_path / "exhausted"
    exhausted_dir.mkdir()
    out = _run_cycles(wd, exhausted_dir, recent_minutes=(1, 5, 10, 20, 30))
    exceeded = [e for e in out["events"] if e.get("event") == "pg_down_budget_exceeded"]
    assert exceeded, f"нет события pg_down_budget_exceeded: {out['events']}"
    assert exceeded[0].get("action") == "none"
    assert exceeded[0].get("restarts_last_hour") == wd.RESTART_BUDGET_PER_HOUR
    assert out["calls"] == [], "кластер не должен подниматься при исчерпанном бюджете"
    assert out["state"].get("budget_exceeded") is True
    assert "budget" in out["log"]

    available_dir = tmp_path / "available"
    available_dir.mkdir()
    ok = _run_cycles(wd, available_dir, recent_minutes=(90,))
    assert ok["calls"] == [
        "start_cluster"
    ], f"ожидался подъём кластера, вызовы={ok['calls']}"
    assert not ok["state"].get("budget_exceeded")
