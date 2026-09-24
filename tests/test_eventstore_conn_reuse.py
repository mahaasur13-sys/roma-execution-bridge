"""EVENTSTORE-CONN — Fix B: переиспользование соединения в durability/event_store.py.

Дефект: `EventStore._get_conn()` открывал НОВОЕ sqlite3-соединение на каждый
`append`/`replay`/`get_events_for_job` и никогда его не закрывал. Это churn
(open/GC-finalize на каждый вызов), а не детерминированная fd-утечка: замеры
показывали ~500 `sqlite3.connect()` на 500 append при ограниченном (не монотонном)
росте fd. Fix B хранит ОДНО соединение `self._conn` на весь жизненный цикл стора
(образец — `durability/event_sourcing.py::SQLiteEventStore`) и закрывает его в `close()`.

Эти тесты — реальная регресс-защита: считают вызовы `sqlite3.connect`, рост fd,
живые объекты-соединения, ResourceWarning и roundtrip.
"""

from __future__ import annotations

import gc
import os
import sqlite3
import sys
import warnings

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import durability.event_store as event_store  # noqa: E402
from durability.event_store import Event, EventStore, EventType  # noqa: E402

N_APPENDS = 500


def _append_batch(es: EventStore, n: int = N_APPENDS, job_id: str = "job-1") -> None:
    for i in range(n):
        es.append(
            Event(
                event_type=EventType.JOB_QUEUED.value,
                job_id=job_id,
                payload={"i": i},
            )
        )


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_eventstore_single_connection_reused(monkeypatch, tmp_path):
    """500 append должны использовать ровно ОДНО соединение (не 500)."""
    calls = []
    orig_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        calls.append(1)
        return orig_connect(*args, **kwargs)

    monkeypatch.setattr(event_store.sqlite3, "connect", counting_connect)

    es = EventStore(db_path=str(tmp_path / "events.db"))
    _append_batch(es)

    assert len(calls) == 1, f"ожидалось 1 sqlite3.connect, получено {len(calls)}"


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"),
    reason="требуется Linux /proc/self/fd · issue: EVENTSTORE-CONN · expiry: 2026-12-31",
)
def test_eventstore_fd_stable_over_appends(tmp_path):
    """fd не должен расти за 500 append (нет churn-дескрипторов)."""
    es = EventStore(db_path=str(tmp_path / "events.db"))
    fd_after_init = _fd_count()

    _append_batch(es)
    fd_after_appends = _fd_count()

    assert (
        fd_after_appends == fd_after_init
    ), f"fd вырос на {fd_after_appends - fd_after_init} за {N_APPENDS} append"


def test_eventstore_no_stray_connections(tmp_path):
    """После 500 append жив только self._conn — посторонних соединений нет."""
    es = EventStore(db_path=str(tmp_path / "events.db"))
    _append_batch(es)

    gc.collect()
    stray = [
        obj
        for obj in gc.get_objects()
        if isinstance(obj, sqlite3.Connection) and obj is not es._conn
    ]
    assert stray == [], f"обнаружено {len(stray)} посторонних sqlite3-соединений"


def test_eventstore_no_resource_warning_on_close(tmp_path):
    """close() не должен порождать ResourceWarning (незакрытых соединений нет)."""
    es = EventStore(db_path=str(tmp_path / "events.db"))
    _append_batch(es, n=100)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        es.close()
        gc.collect()

    rw = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert rw == [], f"ResourceWarning при close: {[str(w.message) for w in rw]}"


def test_eventstore_roundtrip_and_reopen(tmp_path):
    """append → replay → get_events_for_job → close → повторное открытие."""
    db_path = str(tmp_path / "events.db")
    es = EventStore(db_path=db_path)

    es.append(
        Event(event_type=EventType.JOB_QUEUED.value, job_id="job-1", payload={"a": 1})
    )
    es.append(
        Event(event_type=EventType.JOB_STARTED.value, job_id="job-1", payload={"a": 2})
    )
    es.append(
        Event(
            event_type=EventType.JOB_COMPLETED.value, job_id="job-1", payload={"a": 3}
        )
    )

    assert es.get_latest_sequence() == 3
    all_events = es.replay(0)
    assert [e.event_type for e in all_events] == [
        EventType.JOB_QUEUED.value,
        EventType.JOB_STARTED.value,
        EventType.JOB_COMPLETED.value,
    ]
    assert len(es.get_events_for_job("job-1")) == 3

    es.close()

    # close() теперь реально закрывает соединение — данные должны сохраниться и
    # быть читаемы после повторного открытия (sequence восстанавливается из MAX).
    es2 = EventStore(db_path=db_path)
    assert es2.get_latest_sequence() == 3
    assert len(es2.replay(0)) == 3
