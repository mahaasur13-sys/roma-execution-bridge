"""F02-A + F02-C (OWNER DECISION 2026-11-09): окно list_error в приложении.

Требование F02: >= 3 list_error в скользящем окне 15m, условие непрерывно 5m.
Здесь — сторона приложения: граница окна (O1), zero-init (F02-C), окно после
рестарта (O4), время последней попытки свипера (O8). Без PG, без сети.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from billing import execution_worker as ew


def _val(name: str, labels: dict | None = None) -> float | None:
    return REGISTRY.get_sample_value(name, labels or {})


class _Clock:
    def __init__(self, t: float):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


def _window(start: float = 1_000_000.0):
    from monitoring.refund_error_window import ErrorWindow

    clk = _Clock(start)
    return ErrorWindow(clock=clk), clk


# --- O1: граница (now-900s, now] -------------------------------------------------


def test_window_seconds_is_900():
    from monitoring.refund_error_window import WINDOW_SECONDS

    w, _ = _window()
    assert WINDOW_SECONDS == 900.0
    assert w.window_seconds == 900.0


def test_event_exactly_at_now_minus_900_is_excluded():
    w, clk = _window()
    w.record(clk.t)
    clk.t += 900.0
    assert w.count() == 0


def test_event_at_now_minus_899_is_included():
    w, clk = _window()
    w.record(clk.t)
    clk.t += 899.0
    assert w.count() == 1


def test_event_at_now_is_included_future_is_not_counted():
    w, clk = _window()
    w.record(clk.t)
    w.record(clk.t + 10.0)  # «будущее» событие (скачок часов) — не в текущем счёте
    assert w.count() == 1
    clk.t += 10.0
    assert w.count() == 2


def test_three_events_count_three_and_expire_individually():
    w, clk = _window()
    t0 = clk.t
    for dt in (0.0, 60.0, 120.0):
        w.record(t0 + dt)
    clk.t = t0 + 120.0
    assert w.count() == 3
    clk.t = t0 + 900.0  # первое ровно на now-900 → вне окна
    assert w.count() == 2
    clk.t = t0 + 960.0
    assert w.count() == 1
    clk.t = t0 + 1020.0
    assert w.count() == 0


def test_out_of_order_events_counted_correctly():
    w, clk = _window()
    t0 = clk.t
    w.record(t0 + 100.0)
    w.record(t0 + 0.0)
    clk.t = t0 + 950.0  # t0 и t0+50 вне окна; t0+100 внутри
    assert w.count() == 1


# --- O4: неполное окно после рестарта --------------------------------------------


def test_window_incomplete_until_900s_after_start():
    w, clk = _window()
    assert w.complete() is False
    clk.t += 899.0
    assert w.complete() is False
    clk.t += 1.0
    assert w.complete() is True


def test_restart_loses_history_and_window_is_incomplete_again():
    w, clk = _window()
    for _ in range(3):
        w.record()
    clk.t += 1000.0
    w.record()
    w.record()
    w.record()
    assert w.count() == 3 and w.complete() is True
    w.reset()  # имитация рестарта процесса
    assert w.count() == 0
    assert w.complete() is False, "после рестарта 0 — нижняя граница, окно не достоверно"
    assert w.started_at == clk.t


# --- F02-C: zero-init + экспорт gauge ------------------------------------------------


def test_zero_init_all_known_outcomes_exported():
    import monitoring.metrics as m

    for outcome in ("ok", "error", "pg_error", "list_error"):
        v = _val("roma_refund_pending_sweep_total", {"outcome": outcome})
        assert v is not None, f"серия outcome={outcome} должна существовать с момента старта (F02-C)"
    assert tuple(m.REFUND_PENDING_SWEEP_OUTCOMES) == ("ok", "error", "pg_error", "list_error")


def test_new_gauges_are_exported():
    import monitoring.metrics  # noqa: F401

    for name in (
        "roma_refund_pending_errors_15m",
        "roma_refund_pending_errors_window_complete",
        "roma_refund_pending_window_start_timestamp_seconds",
        "roma_refund_pending_sweep_last_attempt_timestamp_seconds",
    ):
        assert _val(name) is not None, name
    assert _val("roma_refund_pending_window_start_timestamp_seconds") > 0


# --- Свипер: list_error → окно + счётчик; O8 last_attempt при любом исходе ---------


class _FailingAdapter:
    def get_refund_pending_jobs(self, limit=50):
        raise RuntimeError("db down")


class _EmptyAdapter:
    def get_refund_pending_jobs(self, limit=50):
        return []


@pytest.fixture
def fresh_window(monkeypatch):
    from monitoring import refund_error_window as rew

    rew.refund_list_error_window.reset()
    yield rew.refund_list_error_window
    rew.refund_list_error_window.reset()


def test_list_error_feeds_window_gauge_and_counter(monkeypatch, fresh_window):
    monkeypatch.setattr(ew, "_db_adapter", _FailingAdapter())
    before = _val("roma_refund_pending_sweep_total", {"outcome": "list_error"}) or 0.0
    for _ in range(3):
        out = ew.sweep_refund_pending_once()
        assert out["list_error"] == 1, out
    after = _val("roma_refund_pending_sweep_total", {"outcome": "list_error"})
    assert after - before == 3.0
    assert _val("roma_refund_pending_errors_15m") == 3.0
    assert _val("roma_refund_pending_errors_window_complete") == 0.0  # только что «стартовали»


def test_last_attempt_updated_on_error(monkeypatch, fresh_window):
    monkeypatch.setattr(ew, "_db_adapter", _FailingAdapter())
    monkeypatch.setattr(ew.time, "time", lambda: 1_800_000_000.0)
    ew.sweep_refund_pending_once()
    assert _val("roma_refund_pending_sweep_last_attempt_timestamp_seconds") == 1_800_000_000.0


def test_last_attempt_updated_on_success(monkeypatch, fresh_window):
    monkeypatch.setattr(ew, "_db_adapter", _EmptyAdapter())
    monkeypatch.setattr(ew.time, "time", lambda: 1_800_000_123.0)
    out = ew.sweep_refund_pending_once()
    assert out == {"ok": 0, "error": 0, "pg_error": 0, "list_error": 0}
    assert _val("roma_refund_pending_sweep_last_attempt_timestamp_seconds") == 1_800_000_123.0
    # успешная попытка не пишет событие в окно ошибок (O8 ≠ счётчик событий)
    assert _val("roma_refund_pending_errors_15m") == 0.0


def test_last_attempt_updated_on_unexpected_exception(monkeypatch, fresh_window):
    class _Boom:
        def get_refund_pending_jobs(self, limit=50):
            return [{"id": "j1", "tenant_id": "t1", "error": "refund_pending:debit_exists:0.001"}]

    import main as main_mod

    monkeypatch.setattr(ew, "_db_adapter", _Boom())
    monkeypatch.setattr(main_mod, "refund_job", lambda *a, **k: None)

    class _Abort(BaseException):
        """BaseException — не ловится `except Exception` в цикле свипера."""

    def _upd(*a, **k):
        raise _Abort()

    monkeypatch.setattr(_Boom, "update_execution_job", staticmethod(_upd), raising=False)
    monkeypatch.setattr(ew.time, "time", lambda: 1_800_000_456.0)
    with pytest.raises(_Abort):
        ew.sweep_refund_pending_once()
    assert _val("roma_refund_pending_sweep_last_attempt_timestamp_seconds") == 1_800_000_456.0


# --- Правило: F02-D не используется, O2 сумма ----------------------------------------


def test_alert_rule_uses_app_gauge_sum_and_for_5m():
    from pathlib import Path

    import yaml

    p = Path(__file__).resolve().parent.parent / "deploy" / "monitoring" / "alert-rules-roma.yml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    rules = {r.get("alert"): r for g in doc["groups"] for r in g["rules"] if r.get("alert")}
    r = rules["ROMARefundPendingListError"]
    assert r["expr"].strip() == "sum(roma_refund_pending_errors_15m) >= 3"
    assert r["for"] == "5m"
    assert "offset" not in r["expr"] and "increase(" not in r["expr"]  # O6: без F02-D
    assert "ROMARefundPendingErrorsObservationLost" in rules  # O7
    stale = rules["ROMARefundPendingSweepStale"]  # O8: алерт есть, порог/for — плейсхолдеры (OPEN)
    assert "roma_refund_pending_sweep_last_attempt_timestamp_seconds" in stale["expr"]
    assert stale["labels"]["severity"] == "warning"
    assert "NOT evidence of health" in stale["annotations"]["description"]
