"""core.jev_shadow_log checks (hermetic — writes only to tmp).

Named ``jev_shadow_log_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide
canon run (``.ci/run-completeness.json``, ``collected == 486``) must not shift.
Run explicitly:

    .venv/bin/python -m pytest core/jev_shadow_log_checks.py -q
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import core.jev_shadow_log as shadow_log  # noqa: E402
from core.jev_shadow_log import iter_records, record  # noqa: E402


def test_record_and_iter_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_SHADOW_LOG", str(tmp_path / "shadow.jsonl"))
    record("R1", choice="anomaly", confidence=0.9, verdict="allowed", observed_at="t1")
    record("R5", choice="billing", confidence=0.7, verdict="billing", observed_at="t2")

    recs = list(iter_records(tmp_path / "shadow.jsonl"))
    assert len(recs) == 2
    assert recs[0]["point"] == "R1"
    assert recs[0]["choice"] == "anomaly"
    assert recs[1]["point"] == "R5"
    assert recs[1]["confidence"] == 0.7


def test_iter_records_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_SHADOW_LOG", str(tmp_path / "nope.jsonl"))
    assert list(iter_records()) == []

def _journal_lines(live: Path) -> list[str]:
    """Живой файл + бэкапы ``.1..N`` — все непустые строки журнала."""
    files = [live, *sorted(live.parent.glob(live.name + ".*"))]
    out: list[str] = []
    for path in files:
        out += [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


def test_default_limits_and_env_fallback(monkeypatch):
    monkeypatch.delenv("JEV_SHADOW_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("JEV_SHADOW_LOG_BACKUPS", raising=False)
    assert shadow_log._max_bytes() == 50 * 1024 * 1024
    assert shadow_log._backups() == 5

    monkeypatch.setenv("JEV_SHADOW_LOG_MAX_BYTES", "abc")
    monkeypatch.setenv("JEV_SHADOW_LOG_BACKUPS", "0")
    assert shadow_log._max_bytes() == 50 * 1024 * 1024
    assert shadow_log._backups() == 5


def test_rotation_triggers_at_threshold(tmp_path, monkeypatch):
    live = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("JEV_SHADOW_LOG", str(live))
    monkeypatch.setenv("JEV_SHADOW_LOG_MAX_BYTES", "300")
    monkeypatch.setenv("JEV_SHADOW_LOG_BACKUPS", "5")

    for i in range(12):
        record("R1", n=i, filler="x" * 60)

    assert (tmp_path / "shadow.jsonl.1").exists()
    assert live.stat().st_size <= 300


def test_backups_never_exceed_limit(tmp_path, monkeypatch):
    live = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("JEV_SHADOW_LOG", str(live))
    monkeypatch.setenv("JEV_SHADOW_LOG_MAX_BYTES", "150")
    monkeypatch.setenv("JEV_SHADOW_LOG_BACKUPS", "3")

    for i in range(50):
        record("R1", n=i, filler="x" * 40)

    assert sorted(p.name for p in tmp_path.glob("shadow.jsonl*")) == [
        "shadow.jsonl",
        "shadow.jsonl.1",
        "shadow.jsonl.2",
        "shadow.jsonl.3",
    ]


def test_no_records_lost_when_backups_suffice(tmp_path, monkeypatch):
    live = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("JEV_SHADOW_LOG", str(live))
    monkeypatch.setenv("JEV_SHADOW_LOG_MAX_BYTES", "200")
    monkeypatch.setenv("JEV_SHADOW_LOG_BACKUPS", "40")

    for i in range(40):
        record("R1", n=i, filler="x" * 20)

    lines = _journal_lines(live)
    assert len(lines) == 40  # бэкапов хватило — ни одна запись не потеряна
    assert sorted(json.loads(ln)["n"] for ln in lines) == list(range(40))


def test_rotation_failure_keeps_appending_and_throttles_warning(tmp_path, monkeypatch, caplog):
    live = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("JEV_SHADOW_LOG", str(live))
    monkeypatch.setenv("JEV_SHADOW_LOG_MAX_BYTES", "150")
    monkeypatch.setenv("JEV_SHADOW_LOG_BACKUPS", "2")
    monkeypatch.setattr(shadow_log, "_rotate_warn_ts", 0.0)

    def _boom(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr("core.jev_shadow_log.os.replace", _boom)
    with caplog.at_level(logging.WARNING, logger="core.jev_shadow_log"):
        for i in range(6):
            record("R1", n=i, filler="x" * 40)

    assert len(_journal_lines(live)) == 6  # запись добавлена, исключения нет
    assert not (tmp_path / "shadow.jsonl.1").exists()
    warned = [r for r in caplog.records if "rotation failed" in r.getMessage()]
    assert len(warned) == 1  # throttle: не чаще раза в 60 с


def test_concurrent_writes_are_valid_json(tmp_path, monkeypatch):
    live = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("JEV_SHADOW_LOG", str(live))
    monkeypatch.setenv("JEV_SHADOW_LOG_MAX_BYTES", "2000")
    monkeypatch.setenv("JEV_SHADOW_LOG_BACKUPS", "50")

    def _worker(tag: str) -> None:
        for i in range(50):
            record("R5", tag=tag, n=i, filler="y" * 30)

    threads = [threading.Thread(target=_worker, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    recs = [json.loads(ln) for ln in _journal_lines(live)]
    assert len(recs) == 100  # 2 × 50, строки не потеряны и не склеены
    assert sum(1 for r in recs if r["tag"] == "a") == 50
    assert sum(1 for r in recs if r["tag"] == "b") == 50
