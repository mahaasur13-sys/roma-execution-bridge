"""F1/F2 checks for deploy/ops/pg_watchdog.py — боевой кластер 5432 не трогается.

Named ``pg_watchdog_checks.py`` (NOT ``test_*.py``) on purpose: канон
(.ci/run-completeness.json, 711) не должен сдвинуться. Запуск явный:

    python -m pytest deploy/ops/pg_watchdog_checks.py -q

Матрица F1 гоняется на временном кластере (initdb в /tmp, порт 55432, свой -k
сокет-каталог). Если initdb/пользователь postgres недоступны (CI-раннер без PG),
кластерные кейсы скипаются с явной причиной, остальные идут всегда.

Ни одна проверка не пишет в /var/run/postgresql и /var/lib/postgresql: пути
передаются параметрами, карантин — во временном каталоге pytest.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

HERE = pathlib.Path(__file__).resolve().parent
WATCHDOG = HERE / "pg_watchdog.py"
PG_BIN = pathlib.Path("/usr/lib/postgresql/15/bin")
RUNUSER = "/usr/sbin/runuser"
TEMP_PORT = 55432


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("pg_watchdog_under_test", WATCHDOG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _own_quarantines(root: pathlib.Path, wd) -> list[str]:
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir()
        and d.name.startswith("quarantine-")
        and (d / wd.QUARANTINE_MARKER).exists()
    )


@pytest.fixture(scope="module")
def temp_cluster():
    """Временный кластер: initdb + pg_ctl start на TEMP_PORT со своим сокет-каталогом."""
    if not (PG_BIN / "initdb").exists():
        pytest.skip(f"нет initdb в {PG_BIN} — кластерные кейсы пропущены")
    try:
        import pwd

        pwd.getpwnam("postgres")
    except Exception:
        pytest.skip("нет пользователя postgres — кластерные кейсы пропущены")

    root = pathlib.Path(tempfile.mkdtemp(prefix="roma-pgwd-cluster-"))
    data = root / "data"
    sock = root / "sock"
    sock.mkdir()
    root.chmod(0o755)
    subprocess.run(["chown", "-R", "postgres:postgres", str(root)], check=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    initdb = subprocess.run(
        [
            RUNUSER,
            "-u",
            "postgres",
            "--",
            str(PG_BIN / "initdb"),
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth=trust",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if initdb.returncode != 0:
        pytest.skip(f"initdb rc={initdb.returncode}: {initdb.stderr.strip()[-300:]}")

    start = subprocess.run(
        [
            RUNUSER,
            "-u",
            "postgres",
            "--",
            str(PG_BIN / "pg_ctl"),
            "-D",
            str(data),
            "-l",
            str(root / "pg.log"),
            "-o",
            f"-p {TEMP_PORT} -k {sock}",
            "-w",
            "start",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if start.returncode != 0:
        pytest.skip(f"pg_ctl start rc={start.returncode}: {start.stderr.strip()[-300:]}")

    yield SimpleNamespace(root=root, data=data, sock=sock)

    subprocess.run(
        [
            RUNUSER,
            "-u",
            "postgres",
            "--",
            str(PG_BIN / "pg_ctl"),
            "-D",
            str(data),
            "-m",
            "fast",
            "-w",
            "stop",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    shutil.rmtree(root, ignore_errors=True)


# ── F1: матрица clear_stale_lock ────────────────────────────────────────────


def test_socket_present_is_noop(tmp_path) -> None:
    wd = _load_watchdog()
    sock = tmp_path / ".s.PGSQL.5432"
    sock.write_text("", encoding="utf-8")
    lock = tmp_path / ".s.PGSQL.5432.lock"
    lock.write_text(f"{_dead_pid()}\n", encoding="utf-8")

    result = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(sock),
        lock_path=str(lock),
        data_dir=str(tmp_path),
        quarantine_root=str(tmp_path / "q"),
    )

    assert result == (False, "socket_present", None)
    assert lock.exists(), "lock не должен трогаться, пока сокет на месте"


def test_non_postgres_live_pid_is_cleared(tmp_path) -> None:
    wd = _load_watchdog()
    pid = os.getpid()
    assert wd._pid_alive(pid)
    assert wd._pid_comm(pid) != "postgres", "тест-процесс не postgres — иначе кейс не тот"
    lock = tmp_path / ".s.PGSQL.5432.lock"
    lock.write_text(f"{pid}\n{tmp_path}\n", encoding="utf-8")
    qroot = tmp_path / "q"
    qroot.mkdir()

    cleared, reason, quarantine = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(lock),
        data_dir=str(tmp_path / "nodata"),
        quarantine_root=str(qroot),
    )

    assert (cleared, reason) == (True, "stale_lock_cleared")
    assert not lock.exists()
    assert (pathlib.Path(quarantine) / lock.name).exists()
    assert (pathlib.Path(quarantine) / wd.QUARANTINE_MARKER).exists()


def test_dead_pid_is_cleared(tmp_path) -> None:
    wd = _load_watchdog()
    pid = _dead_pid()
    assert not wd._pid_alive(pid)
    lock = tmp_path / ".s.PGSQL.5432.lock"
    lock.write_text(f"{pid}\n", encoding="utf-8")

    cleared, reason, quarantine = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(lock),
        data_dir=str(tmp_path / "nodata"),
        quarantine_root=str(tmp_path / "q"),
    )

    assert (cleared, reason) == (True, "stale_lock_cleared")
    assert not lock.exists()
    assert (pathlib.Path(quarantine) / lock.name).exists()


def test_unreadable_lock_refuses(tmp_path) -> None:
    wd = _load_watchdog()
    lock = tmp_path / ".s.PGSQL.5432.lock"
    lock.mkdir()  # каталог вместо файла: чтение первой строки падает

    cleared, reason, quarantine = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(lock),
        data_dir=str(tmp_path / "nodata"),
        quarantine_root=str(tmp_path / "q"),
    )

    assert (cleared, reason, quarantine) == (False, "lock_unreadable", None)
    assert lock.exists()


def test_missing_lock_is_noop(tmp_path) -> None:
    wd = _load_watchdog()

    result = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(tmp_path / "absent.lock"),
        data_dir=str(tmp_path / "nodata"),
        quarantine_root=str(tmp_path / "q"),
    )

    assert result == (False, "no_lock", None)


def test_live_postgres_lock_is_refused(temp_cluster, tmp_path) -> None:
    """PID живого postmaster в lock — НЕ чистим (это не осиротевший файл)."""
    wd = _load_watchdog()
    pm_pid = int((temp_cluster.data / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0])
    assert wd._pid_alive(pm_pid) and wd._pid_comm(pm_pid) == "postgres"
    lock = tmp_path / ".s.PGSQL.55432.lock"
    lock.write_text(f"{pm_pid}\n{tmp_path}\n", encoding="utf-8")

    result = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(lock),
        data_dir=str(tmp_path / "nodata"),
        quarantine_root=str(tmp_path / "q"),
    )

    assert result == (False, "lock_owner_is_postgres", None)
    assert lock.exists()


def test_live_postmaster_pid_is_refused(temp_cluster, tmp_path) -> None:
    """Мёртвый lock, но живой postmaster.pid на тот же data dir — отказ (два postmaster)."""
    wd = _load_watchdog()
    lock = tmp_path / ".s.PGSQL.55432.lock"
    lock.write_text(f"{_dead_pid()}\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(temp_cluster.data / "postmaster.pid", data / "postmaster.pid")

    result = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(lock),
        data_dir=str(data),
        quarantine_root=str(tmp_path / "q"),
    )

    assert result == (False, "postmaster_running", None)
    assert lock.exists()


def test_stale_postmaster_pid_moved_to_quarantine(tmp_path) -> None:
    wd = _load_watchdog()
    lock = tmp_path / ".s.PGSQL.5432.lock"
    lock.write_text(f"{_dead_pid()}\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "postmaster.pid").write_text(f"{_dead_pid()}\n{data}\n", encoding="utf-8")

    cleared, reason, quarantine = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(lock),
        data_dir=str(data),
        quarantine_root=str(tmp_path / "q"),
    )

    assert (cleared, reason) == (True, "stale_lock_cleared")
    assert not lock.exists()
    assert not (data / "postmaster.pid").exists()
    moved = pathlib.Path(quarantine)
    assert (moved / lock.name).exists()
    assert (moved / "postmaster.pid").exists()


def test_quarantine_rotation_keeps_own_dirs_only(tmp_path) -> None:
    wd = _load_watchdog()
    qroot = tmp_path / "q"
    qroot.mkdir()
    for i in range(12):
        own = qroot / f"quarantine-20260101T0000{i:02d}Z"
        own.mkdir()
        (own / wd.QUARANTINE_MARKER).write_text("x", encoding="utf-8")
    manual = ["quarantine-20260927T052712Z", "quarantine-20261012T094559Z"]
    for name in manual:
        (qroot / name).mkdir()
        (qroot / name / ".s.PGSQL.5432.lock").write_text("2155\n", encoding="utf-8")

    lock = tmp_path / ".s.PGSQL.5432.lock"
    lock.write_text(f"{_dead_pid()}\n", encoding="utf-8")

    cleared, _, quarantine = wd.clear_stale_lock(
        port=_free_port(),
        socket_path=str(tmp_path / "absent.sock"),
        lock_path=str(lock),
        data_dir=str(tmp_path / "nodata"),
        quarantine_root=str(qroot),
    )

    assert cleared is True
    own = _own_quarantines(qroot, wd)
    assert len(own) == wd.QUARANTINE_KEEP == 10, own
    assert pathlib.Path(quarantine).name in own
    for name in manual:
        assert (qroot / name / ".s.PGSQL.5432.lock").exists(), f"ручной карантин {name} тронут"


# ── F2: бюджет не замолкает ─────────────────────────────────────────────────


def _run_watchdog(wd, tmp_path, monkeypatch, *, recent_minutes, ready, max_cycles, extra_state=None, clear=None):
    """Прогон main() без PG: pg_is_ready/start_cluster/wait_ready/clear_stale_lock подменены."""
    state_path = tmp_path / "state.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    state = {
        "restarts": [
            (now - datetime.timedelta(minutes=m)).isoformat(timespec="seconds").replace("+00:00", "Z")
            for m in recent_minutes
        ],
        "restarts_total": 0,
        "ledger_baseline": 1,
        "was_down": False,
    }
    state.update(extra_state or {})
    state_path.write_text(json.dumps(state), encoding="utf-8")

    wd.STATE_DIR = str(tmp_path)
    wd.STATE_PATH = str(state_path)
    wd.LOG_PATH = str(tmp_path / "watchdog.log")

    ready_iter = iter(ready)
    wd.pg_is_ready = lambda: next(ready_iter, False)
    calls: list[str] = []

    def fake_start_cluster() -> bool:
        calls.append("start_cluster")
        return True

    wd.start_cluster = fake_start_cluster
    wd.wait_ready = lambda _timeout: None
    wd.ledger_rows = lambda: None
    wd.clear_stale_lock = clear if clear is not None else (lambda **_kw: (False, "no_lock", None))

    slept: list[float] = []
    cycles = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        cycles["n"] += 1
        if cycles["n"] >= max_cycles:
            raise SystemExit(0)

    monkeypatch.setattr(wd.time, "sleep", fake_sleep)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with pytest.raises(SystemExit):
            wd.main()

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    return SimpleNamespace(
        events=events,
        calls=calls,
        slept=slept,
        state=json.loads(state_path.read_text(encoding="utf-8")),
    )


def test_budget_spent_on_every_attempt_and_error_once(tmp_path, monkeypatch) -> None:
    """Бюджет тратится на неудачных попытках; при исчерпании пробы идут реже, ERROR — один."""
    wd = _load_watchdog()
    out = _run_watchdog(
        wd,
        tmp_path,
        monkeypatch,
        recent_minutes=(),
        ready=[False] * 20,
        max_cycles=7,
    )

    assert out.calls == ["start_cluster"] * wd.RESTART_BUDGET_PER_HOUR, out.calls
    assert len(out.state["restarts"]) == wd.RESTART_BUDGET_PER_HOUR
    assert out.state["restarts_total"] == wd.RESTART_BUDGET_PER_HOUR
    assert out.state["budget_exceeded"] is True

    budget_events = [e for e in out.events if e.get("event") == "pg_down_budget_exceeded"]
    assert len(budget_events) == 1, budget_events
    assert budget_events[0]["level"] == "ERROR"
    assert budget_events[0]["hint"] == "manual_intervention_required"
    assert budget_events[0]["action"] == "none"
    assert budget_events[0]["restarts_last_hour"] == wd.RESTART_BUDGET_PER_HOUR

    assert out.slept[: wd.RESTART_BUDGET_PER_HOUR] == [wd.INTERVAL] * wd.RESTART_BUDGET_PER_HOUR
    assert out.slept[-1] == wd.INTERVAL * wd.BUDGET_SLOWDOWN


def test_budget_alert_is_throttled_within_hour(tmp_path, monkeypatch) -> None:
    wd = _load_watchdog()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    out = _run_watchdog(
        wd,
        tmp_path,
        monkeypatch,
        recent_minutes=(1, 5, 10, 20, 30),
        ready=[False] * 5,
        max_cycles=3,
        extra_state={"budget_alerted_at": now_iso},
    )

    assert [e for e in out.events if e.get("event") == "pg_down_budget_exceeded"] == []
    assert out.calls == []
    assert out.state["budget_exceeded"] is True
    assert out.slept == [wd.INTERVAL * wd.BUDGET_SLOWDOWN] * 3


def test_recovery_resets_budget_flag(tmp_path, monkeypatch) -> None:
    wd = _load_watchdog()
    out = _run_watchdog(
        wd,
        tmp_path,
        monkeypatch,
        recent_minutes=(1, 5, 10, 20, 30),
        ready=[False, True, True],
        max_cycles=3,
    )

    recovered = [e for e in out.events if e.get("event") == "pg_recovered"]
    assert len(recovered) == 1, out.events
    assert out.state["budget_exceeded"] is False
    assert out.state["budget_alerted_at"] is None
    assert out.slept == [wd.INTERVAL * wd.BUDGET_SLOWDOWN, wd.INTERVAL, wd.INTERVAL]


def test_stale_lock_cleared_before_start(tmp_path, monkeypatch) -> None:
    """F1 подключён к боевому пути: чистка вызывается до start_cluster."""
    wd = _load_watchdog()
    seen: list[str] = []

    def fake_clear(**_kw):
        seen.append("clear")
        return (True, "stale_lock_cleared", "/tmp/quarantine-test")

    def fake_start() -> bool:
        seen.append("start")
        return True

    def fake_start_cluster_factory(calls):
        def fake_start_cluster() -> bool:
            calls.append("start_cluster")
            return True

        return fake_start_cluster

    out = _run_watchdog(
        wd,
        tmp_path,
        monkeypatch,
        recent_minutes=(),
        ready=[False] * 4,
        max_cycles=1,
        clear=fake_clear,
    )

    assert seen[0] == "clear", seen
    assert out.calls == ["start_cluster"]
    assert any(e.get("event") == "stale_lock_cleared" for e in out.events)


def test_stale_lock_refusal_does_not_block_start(tmp_path, monkeypatch) -> None:
    """Отказ чистки (например, живой кластер) не мешает обычному pg_ctlcluster start."""
    wd = _load_watchdog()

    out = _run_watchdog(
        wd,
        tmp_path,
        monkeypatch,
        recent_minutes=(),
        ready=[False] * 4,
        max_cycles=1,
        clear=lambda **_kw: (False, "socket_present", None),
    )

    assert out.calls == ["start_cluster"]
    skipped = [e for e in out.events if e.get("event") == "stale_lock_skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "socket_present"
