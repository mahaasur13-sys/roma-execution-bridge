"""A-0: негативный тест детектора коллизии сессий.

Доктрина аудитора: «у каждого детектора есть НЕГАТИВНЫЙ тест, доказывающий, что он может сработать;
детектор без такого теста считается несуществующим». Здесь проверяется именно это:

  * два свежих маркера → запись ЗАБЛОКИРОВАНА (CollisionError / exit 3);
  * один свежий маркер → запись разрешена;
  * устаревшие маркеры → не мешают (иначе детектор срабатывал бы на мусоре);
  * heartbeat ОБНОВЛЯЕТ last_seen маркера (дефект P-3: раньше last_seen застывал);
  * fencing_token растёт ТОЛЬКО при capture, не при heartbeat.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
LEASE_PY = REPO_ROOT / "deploy" / "ops" / "lease.py"
REAL_SESSIONS = pathlib.Path("/home/workspace/artifacts/.sessions")


def load_lease_module():
    spec = importlib.util.spec_from_file_location("roma_lease_ops", LEASE_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lease = load_lease_module()


def iso_minutes_ago(minutes: int) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_marker_file(sessions: pathlib.Path, instance_id: str, last_seen: str) -> pathlib.Path:
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
    # stand-down предыдущего писателя = его маркер снят
    (lease_obj.sessions / "instance-a.json").unlink()

    assert lease_obj.capture("instance-b")["fencing_token"] == 2
    assert lease_obj.heartbeat()["fencing_token"] == 2


def test_capture_by_second_writer_while_first_alive_is_visible(tmp_path: pathlib.Path) -> None:
    """Захват вторым экземпляром при живом первом обязан быть наблюдаемым и блокирующим."""
    lease_obj = lease.Lease(tmp_path, ttl_min=15)
    lease_obj.capture("instance-a")
    lease_obj.capture("instance-b")
    assert lease_obj.status()["fresh_session_markers"] == 2
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


def test_real_sessions_dir_has_at_most_one_fresh_marker() -> None:
    """Интеграционная проверка реального каталога: живой писатель должен быть один."""
    if not REAL_SESSIONS.is_dir():
        pytest.skip("issue: A-0 · нет каталога .sessions — проверка неприменима")
    fresh = lease.fresh_markers(REAL_SESSIONS, 15)
    assert len(fresh) <= 1, f"свежих маркеров больше одного: {sorted(fresh)}"
