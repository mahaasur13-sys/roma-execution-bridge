#!/usr/bin/env python3
"""The restore drill must refuse any database name that is not a plain roma_drill_* DB."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pg_restore_drill.sh"

REJECTED = [
    "roma",
    "roma_drill_",
    "roma_drill_x; dropdb roma; #",
    "roma_drill_x y",
    "roma_drill_x#",
    "roma_drill_x$(id)",
    "roma_drill_" + "x" * 70,
]


def _run(tmp_path: Path, drill_db: str, bindir: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, DRILL_DB=drill_db)
    env["ROMA_DR_DRILL_STATE_FILE"] = str(tmp_path / "drill_state.json")
    env["PGDUMP_DIR"] = str(tmp_path / "pgdump")
    if bindir is not None:
        env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("drill_db", REJECTED)
def test_refuses_non_drill_names(tmp_path, drill_db):
    proc = _run(tmp_path, drill_db)

    assert proc.returncode == 2
    assert "REFUSING" in proc.stdout + proc.stderr
    assert not (tmp_path / "drill_state.json").exists()


def test_accepts_plain_drill_name(tmp_path):
    proc = _run(tmp_path, "roma_drill_20261024T000000Z")

    assert proc.returncode != 2
    assert "REFUSING" not in proc.stdout + proc.stderr
    assert "offsite state missing" in proc.stdout


def test_cleanup_skips_dropdb_when_db_never_created(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "runuser.calls"
    stub = bindir / "runuser"
    stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 0\n')
    stub.chmod(0o755)

    proc = _run(tmp_path, "roma_drill_20261024T000000Z", bindir=bindir)

    assert proc.returncode == 1
    assert "no db created" in proc.stdout
    assert not calls.exists()
