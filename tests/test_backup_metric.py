#!/usr/bin/env python3
"""roma_backup_last_success_timestamp is sourced from the pg_dump state file."""

import json
import sys

sys.path.insert(0, ".")

import main
from monitoring.metrics import roma_backup_last_success_timestamp


def _gauge() -> float:
    return roma_backup_last_success_timestamp._value.get()


def test_ok_reads_last_success_ts(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_success_ts": 1792549632, "last_result": "ok"}))
    monkeypatch.setenv("ROMA_BACKUP_STATE_FILE", str(state))

    main._set_backup_metric()

    assert _gauge() == 1792549632.0


def test_missing_file_sets_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMA_BACKUP_STATE_FILE", str(tmp_path / "absent.json"))

    main._set_backup_metric()

    assert _gauge() == 0.0


def test_bad_json_sets_zero(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text("{not json")
    monkeypatch.setenv("ROMA_BACKUP_STATE_FILE", str(state))

    main._set_backup_metric()

    assert _gauge() == 0.0
