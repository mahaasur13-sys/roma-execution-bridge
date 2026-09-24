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


def _db_fd_count(db_path: str) -> int:
    """Число открытых fd, указывающих на файл БД.

    Точнее, чем общий `len(os.listdir('/proc/self/fd'))`: тот счётчик process-wide
    дёргается на ±1 от посторонних fd pytest/рантайма (временные файлы, bytecode),
    а утечка соединений EventStore видна именно как рост fd на сам файл БД.
    """
    n = 0
    fd_dir = "/proc/self/fd"
    for fd in os.listdir(fd_dir):
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if db_path in target:
            n += 1
    return n


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
    """fd на файл БД не должен расти за 500 append (нет churn-дескрипторов)."""
    db_path = str(tmp_path / "events.db")
    es = EventStore(db_path=db_path)
    fd_after_init = _db_fd_count(db_path)

    _append_batch(es)
    fd_after_appends = _db_fd_count(db_path)

    assert (
        fd_after_appends == fd_after_init
    ), f"fd на файл БД вырос на {fd_after_appends - fd_after_init} за {N_APPENDS} append"


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


class _FailingCommitConn:
    """Обёртка над sqlite3.Connection: commit падает заданное число раз.

    `sqlite3.Connection` — иммутабельный C-тип (нельзя monkeypatch'ить ни инстанс,
    ни класс), поэтому провал commit симулируется делегирующей обёрткой: всё, кроме
    `commit`, прозрачно уходит в реальное соединение.
    """

    def __init__(self, conn, fail_times: int):
        self._conn = conn
        self._fail = fail_times

    def commit(self):
        if self._fail > 0:
            self._fail -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_append_commit_failure_rolls_back_and_next_append_clean(tmp_path):
    """Упавший commit обязан откатиться: следующая запись не утащит недописанное.

    (а) после исключения последующий append хранит ТОЛЬКО своё событие;
    (в) повторный append после ошибки не дублирует payload.
    """
    es = EventStore(db_path=str(tmp_path / "events.db"))
    es._conn = _FailingCommitConn(es._conn, fail_times=1)

    with pytest.raises(sqlite3.OperationalError):
        es.append(
            Event(
                event_type=EventType.JOB_QUEUED.value,
                job_id="job-1",
                payload={"i": "bad"},
            )
        )

    es.append(
        Event(
            event_type=EventType.JOB_STARTED.value,
            job_id="job-1",
            payload={"i": "good"},
        )
    )

    rows = es.replay(0)
    assert (
        len(rows) == 1
    ), f"ожидалось ровно 1 событие (откаченного нет), получено {len(rows)}"
    assert rows[0].payload == {"i": "good"}
    assert rows[0].event_type == EventType.JOB_STARTED.value
    # (в) нет дублей: откаченный payload не просочился
    assert [r.payload for r in rows] == [{"i": "good"}]


def test_append_sequence_not_consumed_on_failed_commit(tmp_path):
    """(б) self._sequence двигается только после успешного commit — без дыр."""
    es = EventStore(db_path=str(tmp_path / "events.db"))
    es._conn = _FailingCommitConn(es._conn, fail_times=1)

    with pytest.raises(sqlite3.OperationalError):
        es.append(
            Event(
                event_type=EventType.JOB_QUEUED.value,
                job_id="job-1",
                payload={"i": "bad"},
            )
        )

    # номер неудавшейся записи НЕ съеден
    assert es.get_latest_sequence() == 0

    good = Event(
        event_type=EventType.JOB_STARTED.value,
        job_id="job-1",
        payload={"i": "good"},
    )
    es.append(good)

    assert es.get_latest_sequence() == 1
    assert good.sequence == 1
    rows = es.replay(0)
    assert [r.sequence for r in rows] == [1]


def test_append_rollback_failure_does_not_mask_original_error(tmp_path):
    """Сбой rollback() не должен маскировать исходную ошибку commit()."""

    class CommitBoom(Exception):
        pass

    class RollbackBoom(Exception):
        pass

    es = EventStore(db_path=str(tmp_path / "e.db"))
    real = es._conn
    commit_err = CommitBoom("commit failed")
    rollback_err = RollbackBoom("rollback failed")

    class FlakyConn:
        def __init__(self, real_conn, commit_exc, rollback_exc):
            self._real = real_conn
            self._ce = commit_exc
            self._re = rollback_exc

        def __getattr__(self, name):
            return getattr(self._real, name)

        def commit(self):
            raise self._ce

        def rollback(self):
            raise self._re

    es._conn = FlakyConn(real, commit_err, rollback_err)

    with pytest.raises(CommitBoom) as ei:
        es.append(
            Event(
                event_type=EventType.JOB_QUEUED.value,
                job_id="job-1",
                payload={"i": "bad"},
            )
        )

    # пробрашена именно ИСХОДНАЯ ошибка, а не rollback-овская
    assert ei.value is commit_err
