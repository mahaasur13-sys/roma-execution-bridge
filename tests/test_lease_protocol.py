"""A-0: негативный тест детектора коллизии сессий.

Доктрина аудитора: «у каждого детектора есть НЕГАТИВНЫЙ тест, доказывающий, что он может сработать;
детектор без такого теста считается несуществующим». Здесь проверяется именно это:

  * два свежих маркера → запись ЗАБЛОКИРОВАНА (CollisionError / exit 3);
  * один свежий маркер → запись разрешена;
  * устаревшие маркеры → не мешают (иначе детектор срабатывал бы на мусоре);
  * heartbeat ОБНОВЛЯЕТ last_seen маркера (дефект P-3: раньше last_seen застывал);
  * fencing_token растёт ТОЛЬКО при capture, не при heartbeat.

P-LEASE-3 (ревизии №18/№19): право перезахвата возникает по TTL, а не по отсутствующему PID.
Негативы ниже доказывают, что capture ОТКАЗЫВАЕТ (exit 3) при живой чужой аренде и при
повреждённом heartbeat, оставляя аренду и маркеры побайтово неизменными; протухшая аренда
даёт ровно один успешный захват с token+1.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LEASE_PY = REPO_ROOT / "deploy" / "ops" / "lease.py"


def load_lease_module():
    spec = importlib.util.spec_from_file_location("roma_lease_ops", LEASE_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lease = load_lease_module()


def iso_minutes_ago(minutes: int) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes
    )
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_marker_file(
    sessions: pathlib.Path, instance_id: str, last_seen: str
) -> pathlib.Path:
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{instance_id}.json"
    path.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "first_seen": last_seen,
                "last_seen": last_seen,
                "pid": 1,
                "host": "test",
                "boot_id": "test",
            }
        ),
        encoding="utf-8",
    )
    return path


def write_lease_file(
    root: pathlib.Path,
    *,
    instance_id: str,
    heartbeat_at,
    token: int = 1,
) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".writer_lease.json"
    path.write_text(
        json.dumps(
            {
                "conversation_id": "con-fixture",
                "instance_id": instance_id,
                "fencing_token": token,
                "boot_id": "fixture-boot",
                "owner": "zo-node/con-fixture",
                "pid": 1,
                "host": "fixture",
                "started_at": iso_minutes_ago(60),
                "heartbeat_at": heartbeat_at,
                "ttl_min": 15,
                "scope": [],
                "task": "fixture",
                "status": "ACTIVE — fixture",
            }
        ),
        encoding="utf-8",
    )
    return path


def expire_lease(root: pathlib.Path, minutes: int = 40) -> None:
    """Состарить аренду: heartbeat_at в прошлом (TTL истёк)."""
    path = root / ".writer_lease.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat_at"] = iso_minutes_ago(minutes)
    path.write_text(json.dumps(data), encoding="utf-8")


def age_out(root: pathlib.Path, minutes: int = 40) -> None:
    """Ушедший писатель: heartbeat и маркеры стареют ВМЕСТЕ (A-0: они обновляются парой)."""
    expire_lease(root, minutes)
    sessions = root / ".sessions"
    if sessions.is_dir():
        for path in sessions.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["last_seen"] = iso_minutes_ago(minutes)
            path.write_text(json.dumps(data), encoding="utf-8")


def state_snapshot(root: pathlib.Path) -> dict:
    """Побайтовый снимок аренды и маркеров (файл блокировки арендой не считается)."""
    files = []
    lease_file = root / ".writer_lease.json"
    if lease_file.exists():
        files.append(lease_file)
    sessions = root / ".sessions"
    if sessions.is_dir():
        files.extend(sorted(sessions.glob("*.json")))
    return {str(p.relative_to(root)): p.read_bytes() for p in files}


def test_two_fresh_markers_block_writing(tmp_path: pathlib.Path) -> None:
    sessions = tmp_path / ".sessions"
    write_marker_file(sessions, "instance-a", lease.iso())
    write_marker_file(sessions, "instance-b", lease.iso())

    # владелец пишет со своим instance_id — чужой свежий маркер обязан заблокировать
    with pytest.raises(lease.CollisionError) as excinfo:
        lease.guard(sessions, 15, own_instance="instance-a")
    assert "LEASE COLLISION" in str(excinfo.value)
    assert "instance-b" in str(excinfo.value)

    # без указания своего instance — блокировка тем более
    with pytest.raises(lease.CollisionError):
        lease.guard(sessions, 15)


def test_single_fresh_marker_allows_writing(tmp_path: pathlib.Path) -> None:
    sessions = tmp_path / ".sessions"
    write_marker_file(sessions, "instance-a", lease.iso())
    assert lease.guard(sessions, 15, own_instance="instance-a") != {}
    assert lease.guard(sessions, 15) != {}


def test_stale_markers_do_not_block(tmp_path: pathlib.Path) -> None:
    sessions = tmp_path / ".sessions"
    write_marker_file(sessions, "instance-fresh", lease.iso())
    write_marker_file(sessions, "instance-stale", iso_minutes_ago(40))
    fresh = lease.guard(sessions, 15, own_instance="instance-fresh")
    assert set(fresh) == {"instance-fresh"}
    assert lease.fresh_markers(sessions, 15).keys() == {"instance-fresh"}


def test_heartbeat_refreshes_marker_last_seen(tmp_path: pathlib.Path) -> None:
    lease_obj = lease.Lease(tmp_path, ttl_min=15)
    state = lease_obj.capture("instance-a")
    marker = lease_obj.sessions / "instance-a.json"

    stale = iso_minutes_ago(90)
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["last_seen"] = stale
    marker.write_text(json.dumps(data), encoding="utf-8")
    assert lease.fresh_markers(lease_obj.sessions, 15) == {}

    lease_obj.heartbeat()

    refreshed = json.loads(marker.read_text(encoding="utf-8"))
    assert refreshed["last_seen"] != stale
    assert refreshed["last_seen"] >= state["started_at"]
    assert refreshed["pid"] > 0 and refreshed["boot_id"]
    assert lease.fresh_markers(lease_obj.sessions, 15) != {}
    assert list(lease_obj.sessions.glob("*.tmp")) == []


def test_fencing_token_grows_only_on_capture(tmp_path: pathlib.Path) -> None:
    lease_obj = lease.Lease(tmp_path, ttl_min=15)
    assert lease_obj.capture("instance-a")["fencing_token"] == 1
    assert lease_obj.heartbeat()["fencing_token"] == 1
    # stand-down предыдущего писателя = его маркер снят и аренда состарена (P-LEASE-3)
    (lease_obj.sessions / "instance-a.json").unlink()
    age_out(tmp_path)

    assert lease_obj.capture("instance-b")["fencing_token"] == 2
    assert lease_obj.heartbeat()["fencing_token"] == 2


def test_capture_while_foreign_writer_alive_is_refused(tmp_path: pathlib.Path) -> None:
    """НЕГАТИВ P-LEASE-3: живая чужая аренда → отказ, ничего не записано, exit 3."""
    lease_obj = lease.Lease(tmp_path, ttl_min=15)
    lease_obj.capture("instance-a")

    # наследуемые поля stand-down (P-LEASE-1) НЕ должны открывать дыру обхода
    raw = json.loads((tmp_path / ".writer_lease.json").read_text(encoding="utf-8"))
    raw["released_at"] = "2026-09-21T05:02:16Z"
    raw["previous_status"] = "RELEASED — standdown"
    (tmp_path / ".writer_lease.json").write_text(json.dumps(raw), encoding="utf-8")
    before = state_snapshot(tmp_path)

    with pytest.raises(lease.CollisionError) as excinfo:
        lease_obj.capture("instance-b")
    assert "CAPTURE REFUSED" in str(excinfo.value)
    assert "instance-a" in str(excinfo.value)
    assert state_snapshot(tmp_path) == before
    assert not (lease_obj.sessions / "instance-b.json").exists()
    assert lease_obj.state["fencing_token"] == 1
    assert lease_obj.state["instance_id"] == "instance-a"

    assert lease.main(["capture", "instance-c", "--artifacts-dir", str(tmp_path)]) == 3
    assert state_snapshot(tmp_path) == before


def test_holder_recaptures_own_live_lease(tmp_path: pathlib.Path) -> None:
    """Держатель продлевает СВОЮ аренду — отказ касается только чужого захвата."""
    lease_obj = lease.Lease(tmp_path, ttl_min=15)
    lease_obj.capture("instance-a")
    state = lease_obj.capture("instance-a")
    assert state["fencing_token"] == 2
    assert state["instance_id"] == "instance-a"


def test_heartbeat_refuses_when_two_fresh_markers(tmp_path: pathlib.Path) -> None:
    """A-0 сохраняется: два свежих маркера → heartbeat блокируется."""
    lease_obj = lease.Lease(tmp_path, ttl_min=15)
    lease_obj.capture("instance-a")
    write_marker_file(lease_obj.sessions, "instance-b", lease.iso())
    with pytest.raises(lease.CollisionError):
        lease_obj.heartbeat()


def test_cli_guard_exits_3_on_collision(tmp_path: pathlib.Path) -> None:
    sessions = tmp_path / ".sessions"
    write_marker_file(sessions, "instance-a", lease.iso())
    write_marker_file(sessions, "instance-b", lease.iso())
    rc = lease.main(["guard", "--artifacts-dir", str(tmp_path)])
    assert rc == 3

    (sessions / "instance-b.json").unlink()
    assert lease.main(["guard", "--artifacts-dir", str(tmp_path)]) == 0


def test_lease_sessions_dir_has_at_most_one_fresh_marker(
    tmp_path: pathlib.Path,
) -> None:
    """Свой каталог сессий вместо нодового .sessions: проверка обязана исполняться
    и на ноде, и в CI (нодовые артефакты — не часть доказательства).

    Свидетельство то же, что и раньше: живой писатель в каталоге ровно один —
    единственный свежий маркер; протухший (40 минут) в счёт не идёт.
    """
    sessions = tmp_path / ".sessions"
    write_marker_file(sessions, "instance-live", lease.iso())
    write_marker_file(sessions, "instance-stale", iso_minutes_ago(40))
    fresh = lease.fresh_markers(sessions, 15)
    assert len(fresh) <= 1, f"свежих маркеров больше одного: {sorted(fresh)}"
    assert set(fresh) == {"instance-live"}, f"свежий маркер подменён: {sorted(fresh)}"


@pytest.mark.parametrize(
    "broken", ["not-a-timestamp", "", "2026-13-45T99:00:00Z", None]
)
def test_capture_fails_closed_on_broken_heartbeat(
    tmp_path: pathlib.Path, broken
) -> None:
    """НЕГАТИВ P-LEASE-3: битый heartbeat → отказ (не «протух»), аренда не перезаписывается."""
    write_lease_file(tmp_path, instance_id="instance-a", heartbeat_at=broken)
    write_marker_file(tmp_path / ".sessions", "instance-a", iso_minutes_ago(40))
    before = state_snapshot(tmp_path)

    with pytest.raises(lease.CollisionError) as excinfo:
        lease.Lease(tmp_path, ttl_min=15).capture("instance-b")
    assert "heartbeat" in str(excinfo.value)
    assert state_snapshot(tmp_path) == before

    assert lease.main(["capture", "instance-b", "--artifacts-dir", str(tmp_path)]) == 3
    assert state_snapshot(tmp_path) == before


def test_capture_refused_on_fresh_foreign_marker(tmp_path: pathlib.Path) -> None:
    """НЕГАТИВ P-LEASE-3: аренда протухла, но живой чужой маркер → отказ."""
    write_lease_file(
        tmp_path, instance_id="instance-dead", heartbeat_at=iso_minutes_ago(40)
    )
    write_marker_file(tmp_path / ".sessions", "instance-dead", iso_minutes_ago(40))
    write_marker_file(tmp_path / ".sessions", "instance-alive", lease.iso())
    before = state_snapshot(tmp_path)

    with pytest.raises(lease.CollisionError) as excinfo:
        lease.Lease(tmp_path, ttl_min=15).capture("instance-c")
    assert "instance-alive" in str(excinfo.value)
    assert state_snapshot(tmp_path) == before
    assert not (tmp_path / ".sessions" / "instance-c.json").exists()


def test_capture_after_ttl_succeeds_once_with_token_plus_one(
    tmp_path: pathlib.Path,
) -> None:
    """Протухшая аренда: ровно один успешный capture, token+1, старый маркер сохранён."""
    lease_obj = lease.Lease(tmp_path, ttl_min=15)
    lease_obj.capture("instance-a")
    age_out(tmp_path)
    old_marker = (lease_obj.sessions / "instance-a.json").read_bytes()

    state = lease_obj.capture("instance-b")
    assert state["fencing_token"] == 2
    assert state["instance_id"] == "instance-b"
    assert (lease_obj.sessions / "instance-b.json").exists()
    assert (lease_obj.sessions / "instance-a.json").read_bytes() == old_marker

    after = state_snapshot(tmp_path)
    with pytest.raises(lease.CollisionError):
        lease_obj.capture("instance-c")
    assert state_snapshot(tmp_path) == after
    assert lease_obj.state["instance_id"] == "instance-b"


def test_two_concurrent_captures_only_one_succeeds(tmp_path: pathlib.Path) -> None:
    """Конкурентность: под flock ровно один capture получает перо, token растёт ровно раз."""
    cmd = [sys.executable, str(LEASE_PY), "capture", "--artifacts-dir", str(tmp_path)]
    procs = [
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    outs = [p.communicate() for p in procs]
    rcs = sorted(p.returncode for p in procs)
    assert rcs == [0, 3], f"ожидался ровно один успех, получено {rcs}: {outs}"

    winner = json.loads((tmp_path / ".writer_lease.json").read_text(encoding="utf-8"))
    assert winner["fencing_token"] == 1
    markers = sorted(p.name for p in (tmp_path / ".sessions").glob("*.json"))
    assert markers == [f"{winner['instance_id']}.json"], markers
