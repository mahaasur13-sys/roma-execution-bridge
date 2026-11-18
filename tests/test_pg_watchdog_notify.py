"""P2: исчерпанный бюджет рестартов PG обязан доходить до человека, а не только до журнала.

До фикса `pg_down_budget_exceeded` (deploy/ops/pg_watchdog.py, ветка budget_exceeded) писался
только в stdout/watchdog.log: ни одно правило алертов на него не смотрит, а сам сторож
никуда не слал — отказ PG без автоподъёма проходил молча (fail-open).

Контракт канала — тот же, что у deploy-агента: POST JSON в alert-relay
(`ROMA_ALERT_RELAY_URL`, по умолчанию http://127.0.0.1:8099/notify), формат Alertmanager-like
(`title`, `status`, `alerts[].labels.alertname`, `alerts[].annotations.summary`).

Офлайн: PostgreSQL не вызывается (pg_is_ready/start_cluster/... подменены), relay — локальный
HTTP-сервер на 127.0.0.1 со случайным портом; «недоступный relay» — закрытый порт.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import pathlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CANON = REPO_ROOT / "deploy" / "ops" / "pg_watchdog.py"
FULL_BUDGET = (1, 5, 10, 20, 30)  # минуты назад: 5 попыток за час = бюджет исчерпан


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("pg_watchdog_notify_under_test", CANON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(minutes_ago: float) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


@pytest.fixture()
def relay():
    """Локальный alert-relay: пишет каждое тело POST /notify; код ответа задаётся тестом."""
    received: list[dict] = []
    cfg = {"code": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received.append({"path": self.path, "payload": json.loads(body or b"{}")})
            self.send_response(cfg["code"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_address[1]}/notify", "received": received, "cfg": cfg}
    finally:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/notify"


def _run(wd, tmp_path, monkeypatch, *, url, ready, cycles, state=None):
    """Прогон main() без PG: N циклов, затем SystemExit из подменённого sleep."""
    state_path = tmp_path / "state.json"
    if state is None:
        state = {"restarts": [_iso(m) for m in FULL_BUDGET], "restarts_total": 5, "ledger_baseline": 1}
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(wd, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(wd, "STATE_PATH", str(state_path))
    monkeypatch.setattr(wd, "LOG_PATH", str(tmp_path / "watchdog.log"))
    monkeypatch.setattr(wd, "RELAY_URL", url, raising=False)  # на базе без фикса атрибута нет
    ready_iter = iter(ready)
    monkeypatch.setattr(wd, "pg_is_ready", lambda: next(ready_iter, False))
    monkeypatch.setattr(wd, "start_cluster", lambda: True)
    monkeypatch.setattr(wd, "wait_ready", lambda _t: None)
    monkeypatch.setattr(wd, "ledger_rows", lambda: None)
    monkeypatch.setattr(wd, "clear_stale_lock", lambda **_kw: (False, "no_lock", None))
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= cycles:
            raise SystemExit(0)

    monkeypatch.setattr(wd.time, "sleep", fake_sleep)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        wd.main()
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    return events, slept, json.loads(state_path.read_text(encoding="utf-8"))


def _of(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


def test_budget_exceeded_notifies_relay_as_critical(relay, tmp_path, monkeypatch) -> None:
    wd = _load_watchdog()
    events, _slept, _state = _run(wd, tmp_path, monkeypatch, url=relay["url"], ready=[False] * 3, cycles=3)

    assert len(_of(events, "pg_down_budget_exceeded")) == 1
    assert len(relay["received"]) == 1, "исчерпанный бюджет не дошёл до alert-relay (fail-open)"
    req = relay["received"][0]
    assert req["path"] == "/notify"
    payload = req["payload"]
    assert payload["status"] == "firing"
    alert = payload["alerts"][0]
    assert alert["status"] == "firing"
    assert alert["labels"]["alertname"] == "PgWatchdogBudgetExceeded"
    assert alert["labels"]["severity"] == "critical"
    assert "manual" in alert["annotations"]["summary"]
    assert len(_of(events, "notify_sent")) == 1


def test_one_notify_per_episode_even_after_hourly_error_repeat(relay, tmp_path, monkeypatch) -> None:
    wd = _load_watchdog()
    _events, _slept, state = _run(wd, tmp_path, monkeypatch, url=relay["url"], ready=[False] * 3, cycles=3)
    assert len(relay["received"]) == 1

    # Тот же эпизод час спустя: ERROR в журнал повторяется (троттлинг F2), уведомление — нет.
    state["budget_alerted_at"] = _iso(61)
    state["restarts"] = [_iso(m) for m in FULL_BUDGET]
    wd2 = _load_watchdog()
    events2, _slept2, _state2 = _run(
        wd2, tmp_path, monkeypatch, url=relay["url"], ready=[False] * 3, cycles=3, state=state
    )
    assert len(_of(events2, "pg_down_budget_exceeded")) == 1
    assert len(relay["received"]) == 1, relay["received"]


@pytest.mark.parametrize("unreachable", [True, False], ids=["closed_port", "relay_http_502"])
def test_relay_failure_does_not_stop_watchdog_and_is_logged(relay, tmp_path, monkeypatch, unreachable) -> None:
    wd = _load_watchdog()
    relay["cfg"]["code"] = 502
    url = _closed_port_url() if unreachable else relay["url"]
    events, slept, state = _run(wd, tmp_path, monkeypatch, url=url, ready=[False] * 4, cycles=4)

    assert len(slept) == 4, "сторож обязан продолжать циклы при недоступном relay"
    failed = _of(events, "notify_failed")
    assert len(failed) == 1, events
    assert failed[0]["level"] == "ERROR"
    assert failed[0]["alertname"] == "PgWatchdogBudgetExceeded"
    assert not state.get("budget_notified"), "неудачная отправка не должна помечать эпизод как уведомлённый"


def test_failed_notify_is_retried_on_next_hourly_error(relay, tmp_path, monkeypatch) -> None:
    wd = _load_watchdog()
    _e, _s, state = _run(wd, tmp_path, monkeypatch, url=_closed_port_url(), ready=[False] * 2, cycles=2)
    assert relay["received"] == []
    state["budget_alerted_at"] = _iso(61)
    state["restarts"] = [_iso(m) for m in FULL_BUDGET]
    events, _s2, state2 = _run(
        _load_watchdog(), tmp_path, monkeypatch, url=relay["url"], ready=[False] * 2, cycles=2, state=state
    )
    assert len(relay["received"]) == 1
    assert len(_of(events, "notify_sent")) == 1
    assert state2["budget_notified"] is True


def test_recovery_after_notified_episode_sends_resolved(relay, tmp_path, monkeypatch) -> None:
    wd = _load_watchdog()
    events, _slept, state = _run(wd, tmp_path, monkeypatch, url=relay["url"], ready=[False, True, True], cycles=3)

    assert len(_of(events, "pg_recovered")) == 1
    statuses = [r["payload"]["status"] for r in relay["received"]]
    assert statuses == ["firing", "resolved"], statuses
    assert relay["received"][1]["payload"]["alerts"][0]["labels"]["alertname"] == "PgWatchdogBudgetExceeded"
    assert state["budget_notified"] is False
    assert not state.get("resolve_pending")


def test_undelivered_firing_means_no_resolved(relay, tmp_path, monkeypatch) -> None:
    """resolved шлётся только по доставленному firing: иначе человек получил бы «отбой» без тревоги."""
    _e, _s, state = _run(_load_watchdog(), tmp_path, monkeypatch, url=_closed_port_url(), ready=[False], cycles=1)
    assert not state.get("budget_notified")
    events, _s2, state2 = _run(
        _load_watchdog(), tmp_path, monkeypatch, url=relay["url"], ready=[True, True], cycles=2, state=state
    )
    assert len(_of(events, "pg_recovered")) == 1
    assert relay["received"] == []
    assert not state2.get("resolve_pending")


def test_resolved_is_retried_until_delivered(relay, tmp_path, monkeypatch) -> None:
    _e, _s, state = _run(_load_watchdog(), tmp_path, monkeypatch, url=relay["url"], ready=[False], cycles=1)
    assert [r["payload"]["status"] for r in relay["received"]] == ["firing"]

    # PG поднялся, но relay лежит: resolved не потерян, а ждёт.
    events, _s, state = _run(
        _load_watchdog(), tmp_path, monkeypatch, url=_closed_port_url(), ready=[True], cycles=1, state=state
    )
    assert [e["status"] for e in _of(events, "notify_failed")] == ["resolved"]
    assert state["resolve_pending"] is True

    # Раньше NOTIFY_RETRY_S повтора нет …
    _e, _s, state = _run(_load_watchdog(), tmp_path, monkeypatch, url=relay["url"], ready=[True], cycles=1, state=state)
    assert len(relay["received"]) == 1 and state["resolve_pending"] is True

    # … после — доставлен.
    state["notify_attempt_at"] = _iso(6)
    _e, _s, state = _run(_load_watchdog(), tmp_path, monkeypatch, url=relay["url"], ready=[True], cycles=1, state=state)
    assert [r["payload"]["status"] for r in relay["received"]] == ["firing", "resolved"]
    assert state["resolve_pending"] is False


def test_pending_resolved_goes_before_next_episode_firing(relay, tmp_path, monkeypatch) -> None:
    state = {
        "restarts": [_iso(m) for m in FULL_BUDGET],
        "restarts_total": 5,
        "ledger_baseline": 1,
        "resolve_pending": True,
    }
    _e, _s, state = _run(
        _load_watchdog(), tmp_path, monkeypatch, url=relay["url"], ready=[False], cycles=1, state=state
    )
    assert [r["payload"]["status"] for r in relay["received"]] == ["resolved", "firing"]
    assert state["resolve_pending"] is False and state["budget_notified"] is True


def test_firing_retried_every_notify_period_not_only_hourly(relay, tmp_path, monkeypatch) -> None:
    """Недоставленный firing повторяется через NOTIFY_RETRY_S, а не ждёт следующий часовой ERROR."""
    _e, _s, state = _run(_load_watchdog(), tmp_path, monkeypatch, url=_closed_port_url(), ready=[False], cycles=1)
    assert state["budget_alerted_at"]  # часовой ERROR только что был — следующий не раньше чем через час
    state["notify_attempt_at"] = _iso(6)
    state["restarts"] = [_iso(m) for m in FULL_BUDGET]
    events, _s, state = _run(
        _load_watchdog(), tmp_path, monkeypatch, url=relay["url"], ready=[False], cycles=1, state=state
    )
    assert _of(events, "pg_down_budget_exceeded") == []
    assert [r["payload"]["status"] for r in relay["received"]] == ["firing"]
    assert state["budget_notified"] is True


def test_ordinary_restart_does_not_page(relay, tmp_path, monkeypatch) -> None:
    """Обычный pg_down с остатком бюджета чинится сам — в relay ничего не уходит."""
    wd = _load_watchdog()
    state = {"restarts": [], "restarts_total": 0, "ledger_baseline": 1}
    events, _slept, _state = _run(
        wd, tmp_path, monkeypatch, url=relay["url"], ready=[False, True, True], cycles=3, state=state
    )
    assert len(_of(events, "pg_down")) == 1
    assert relay["received"] == []
