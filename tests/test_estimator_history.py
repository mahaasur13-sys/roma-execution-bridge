"""RuntimeEstimator берёт историю из execution_jobs, а не из несуществующего EventStore.

Дефект: `_load_historical_events` вызывал `EventStore.get_all_events()`, которого нет,
и глушил `except Exception: return []` — историческая поправка не работала никогда,
а ошибка не была видна. Здесь проверяется реальный источник и честная деградация.
"""

import sqlite3

import pytest

import db_adapter
from cost.estimator import RuntimeEstimator


def _db(tmp_path, monkeypatch):
    """Временная execution_jobs: 3 completed + 1 running (running обязан быть отброшен)."""
    path = tmp_path / "jobs.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE execution_jobs (id TEXT PRIMARY KEY, tenant_id TEXT, status TEXT,"
        " created_at TEXT, duration_seconds REAL DEFAULT 0.0, started_at TEXT,"
        " completed_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO execution_jobs"
        " (id, tenant_id, status, duration_seconds, started_at, completed_at)"
        " VALUES (?,?,?,?,?,?)",
        [
            # duration_seconds задан — используется он, а не разница времени
            ("a", "t1", "completed", 120.0, "2026-09-20 10:00:00", "2026-09-20 10:02:00"),
            # duration_seconds=0 — берётся completed_at − started_at (30 с, разделители разные)
            ("b", "t1", "completed", 0.0, "2026-09-20 11:00:00", "2026-09-20T11:00:30"),
            # duration_seconds=0 — 60 с
            ("c", "t1", "completed", 0.0, "2026-09-20 12:00:00", "2026-09-20T12:01:00"),
            # незавершённое задание в историю попадать не должно
            ("d", "t1", "running", 999.0, "2026-09-20 13:00:00", None),
        ],
    )
    conn.commit()
    monkeypatch.setattr(db_adapter, "_pg_enabled", lambda: False)
    monkeypatch.setattr(db_adapter, "_sqlite_conn", lambda: sqlite3.connect(path))
    return conn


def test_completed_jobs_duration_and_order(tmp_path, monkeypatch):
    """Только completed; длительность = duration_seconds>0, иначе completed_at−started_at."""
    _db(tmp_path, monkeypatch)

    rows = db_adapter.list_completed_execution_jobs()

    assert [round(r["duration_seconds"], 1) for r in rows] == [60.0, 30.0, 120.0]


def test_hist_avg_is_mean_over_completed(tmp_path, monkeypatch):
    """Среднее по всем завершённым: (60+30+120)/3 = 70 с."""
    _db(tmp_path, monkeypatch)

    estimator = RuntimeEstimator()

    assert estimator._hist_avg() == pytest.approx(70.0, abs=1e-3)


def test_estimate_blends_with_history(tmp_path, monkeypatch):
    """Оценка смешивается 0.7*бенчмарк + 0.3*история — поправка реально применяется."""
    with monkeypatch.context() as m:
        m.setattr(db_adapter, "list_completed_execution_jobs", lambda limit=1000: [])
        baseline = RuntimeEstimator().estimate("ml_training", batch_size=4, model_size="large")
    _db(tmp_path, monkeypatch)
    estimator = RuntimeEstimator()

    blended = estimator.estimate("ml_training", batch_size=4, model_size="large")

    assert estimator.historical_events
    # julianday даёт доли секунды с плавающей погрешностью — сверяем с допуском 1 с
    assert blended["estimated_seconds"] == pytest.approx(
        baseline["estimated_seconds"] * 0.7 + estimator._hist_avg() * 0.3, abs=1
    )


def test_empty_history_keeps_estimate(tmp_path, monkeypatch):
    """Пустая история — оценка без поправки, среднее 0.0."""
    with monkeypatch.context() as m:
        m.setattr(db_adapter, "list_completed_execution_jobs", lambda limit=1000: [])
        baseline = RuntimeEstimator().estimate("inference")

    conn = _db(tmp_path, monkeypatch)
    conn.execute("DELETE FROM execution_jobs")
    conn.commit()
    estimator = RuntimeEstimator()

    assert estimator.historical_events == []
    assert estimator._hist_avg() == 0.0
    assert estimator.estimate("inference")["estimated_seconds"] == baseline["estimated_seconds"]


def test_db_error_logs_warning(monkeypatch):
    """Сбой БД — не молчание: warning в лог и пустая история.

    caplog этого логгера не видит (в проекте свой JSON-обработчик), поэтому
    подменяем сам logger.warning — как в tests/test_nf1_credit_entry_id.py.
    """
    import cost.estimator as estimator_mod

    def boom(limit=1000):
        raise RuntimeError("db is down")

    monkeypatch.setattr(db_adapter, "list_completed_execution_jobs", boom)

    seen: list[str] = []
    real_warning = estimator_mod.logger.warning

    def _spy(msg, *args, **kwargs):
        seen.append(msg % args if args else str(msg))
        return real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(estimator_mod.logger, "warning", _spy)

    estimator = RuntimeEstimator()

    assert estimator.historical_events == []
    assert any("runtime history unavailable" in m for m in seen), seen
    assert any("db is down" in m for m in seen), seen
