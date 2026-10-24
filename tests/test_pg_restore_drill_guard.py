#!/usr/bin/env python3
"""The restore drill must refuse any database name that is not a roma_drill_* DB."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pg_restore_drill.sh"


def _run(tmp_path: Path, drill_db: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DRILL_DB=drill_db)
    env["ROMA_DR_DRILL_STATE_FILE"] = str(tmp_path / "drill_state.json")
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_refuses_live_db_name(tmp_path):
    proc = _run(tmp_path, "roma")

    assert proc.returncode == 2
    assert "roma_drill_" in proc.stdout + proc.stderr
    assert not (tmp_path / "drill_state.json").exists()


def test_refuses_empty_suffix(tmp_path):
    proc = _run(tmp_path, "roma_drill_")

    assert proc.returncode == 2
    assert not (tmp_path / "drill_state.json").exists()
