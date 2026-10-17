"""core.jev_shadow_log checks (hermetic — writes only to tmp).

Named ``jev_shadow_log_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide
canon run (``.ci/run-completeness.json``, ``collected == 486``) must not shift.
Run explicitly:

    .venv/bin/python -m pytest core/jev_shadow_log_checks.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
