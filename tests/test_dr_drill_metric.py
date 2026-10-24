#!/usr/bin/env python3
"""roma_dr_drill_* gauges are sourced from the restore-drill state file."""

import json
import sys

sys.path.insert(0, ".")

import main
from monitoring.metrics import (
    roma_dr_drill_last_success_timestamp,
    roma_dr_drill_rto_seconds,
)


def _gauges() -> tuple:
    return (
        roma_dr_drill_last_success_timestamp._value.get(),
        roma_dr_drill_rto_seconds._value.get(),
    )


def test_ok_reads_last_success_and_rto(tmp_path, monkeypatch):
    state = tmp_path / "drill_state.json"
    state.write_text(
        json.dumps(
            {
                "last_run_ts": 1792900000,
                "last_success_ts": 1792899632,
                "result": "ok",
                "rto_seconds": 42,
                "dump_age_seconds": 3600,
            }
        )
    )
    monkeypatch.setenv("ROMA_DR_DRILL_STATE_FILE", str(state))

    main._set_dr_drill_metrics()

    assert _gauges() == (1792899632.0, 42.0)


def test_missing_file_sets_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("ROMA_DR_DRILL_STATE_FILE", str(tmp_path / "absent.json"))

    main._set_dr_drill_metrics()

    assert _gauges() == (0.0, 0.0)


def test_bad_json_sets_zero(tmp_path, monkeypatch):
    state = tmp_path / "drill_state.json"
    state.write_text("{not json")
    monkeypatch.setenv("ROMA_DR_DRILL_STATE_FILE", str(state))

    main._set_dr_drill_metrics()

    assert _gauges() == (0.0, 0.0)


def test_non_finite_fields_become_zero(tmp_path, monkeypatch):
    state = tmp_path / "drill_state.json"
    state.write_text(json.dumps({"last_success_ts": float("nan"), "rto_seconds": float("inf")}))
    monkeypatch.setenv("ROMA_DR_DRILL_STATE_FILE", str(state))

    main._set_dr_drill_metrics()

    assert _gauges() == (0.0, 0.0)
