"""Operator CLI scripts/roma_hosts_admin.py (memory store; PG variant in test_host_store_pg-style below)."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from gpu_worker import host_bench
from gpu_worker import host_registry as hr
from gpu_worker.host_api import HostApi
from gpu_worker.host_store import MemoryHostStore

ROOT = Path(__file__).resolve().parent.parent


def _cli():
    spec = importlib.util.spec_from_file_location("roma_hosts_admin", ROOT / "scripts" / "roma_hosts_admin.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    monkeypatch.delenv("ROMA_HOSTS_STORE", raising=False)
    return _cli(), MemoryHostStore()


def _reg(m, store, capsys, host_id="felix-3060", own=False):
    argv = [
        "register",
        "--host-id",
        host_id,
        "--gpu-model",
        "RTX_3060",
        "--gpu-count",
        "1",
        "--vram-gb",
        "12",
        "--owner-label",
        "felix",
    ] + (["--own-host"] if own else [])
    assert m.main(argv, store=store) == 0
    return json.loads(capsys.readouterr().out.strip())


def test_register_and_list_without_secrets(cli, capsys, tmp_path):
    m, store = cli
    row = _reg(m, store, capsys)
    assert row["status"] == "pending" and row["owner_label"] == "felix"
    assert m.main(["issue-token", "--host-id", "felix-3060", "--out", str(tmp_path / "t")], store=store) == 0
    capsys.readouterr()
    assert m.main(["list"], store=store) == 0
    out = capsys.readouterr().out
    tok = (tmp_path / "t").read_text().strip()
    digest = store.get_host_token_hash("felix-3060")
    assert "felix-3060" in out and tok not in out and digest not in out and "token" not in out


def test_issue_token_file_0600_not_on_stdout_or_logs(cli, capsys, caplog, tmp_path):
    m, store = cli
    caplog.set_level(logging.DEBUG)
    _reg(m, store, capsys)
    f = tmp_path / "felix.token"
    assert m.main(["issue-token", "--host-id", "felix-3060", "--out", str(f)], store=store) == 0
    cap = capsys.readouterr()
    tok = f.read_text().strip()
    assert len(tok) >= 32
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert tok not in cap.out and tok not in cap.err and tok not in caplog.text
    assert store.get_host_token_hash("felix-3060") not in (tok, None)


def test_issue_token_refuses_overwrite_and_keeps_old(cli, capsys, tmp_path):
    m, store = cli
    _reg(m, store, capsys)
    f = tmp_path / "t"
    f.write_text("existing\n")
    before = store.get_host_token_hash("felix-3060")
    assert m.main(["issue-token", "--host-id", "felix-3060", "--out", str(f)], store=store) == 2
    assert f.read_text() == "existing\n" and store.get_host_token_hash("felix-3060") == before


def test_issue_token_requires_destination_and_print_once_warns(cli, capsys):
    m, store = cli
    _reg(m, store, capsys)
    assert m.main(["issue-token", "--host-id", "felix-3060"], store=store) == 2
    assert m.main(["issue-token", "--host-id", "felix-3060", "--print-once"], store=store) == 0
    cap = capsys.readouterr()
    assert "WARNING" in cap.err and len(cap.out.strip().splitlines()[-1]) >= 32


def test_full_operator_flow_and_revoke_gives_401(cli, capsys, tmp_path):
    m, store = cli
    _reg(m, store, capsys)
    f = tmp_path / "t"
    m.main(["issue-token", "--host-id", "felix-3060", "--out", str(f)], store=store)
    tok = f.read_text().strip()
    th = tmp_path / "th.json"
    th.write_text(json.dumps({"RTX_3060": {"min_tflops": 1e-9, "max_tflops": 1e9}}))
    assert (
        m.main(["start-verification", "--host-id", "felix-3060", "--thresholds", str(th), "--n", "8"], store=store) == 0
    )
    capsys.readouterr()

    reg = hr.HostRegistry(store=store)  # "server" process view of the same store
    reg.load_from_store()
    api = HostApi(reg, store=store)
    job = api.handle({"action": "fetch_job", "host_id": "felix-3060", "token": tok})["job"]
    r = api.handle(
        {
            "action": "submit_result",
            "host_id": "felix-3060",
            "token": tok,
            "job_id": job["job_id"],
            "job_token": job["job_token"],
            "output_sha256": host_bench.reference_hash(job["seed"], 8),
            "elapsed_s": 0.5,
        }
    )
    assert (r["status"], r["trust_tier"]) == ("active", "probation")

    assert m.main(["approve", "--host-id", "felix-3060"], store=store) == 0
    assert json.loads(capsys.readouterr().out)["trust_tier"] == "verified"
    assert m.main(["revoke-token", "--host-id", "felix-3060"], store=store) == 0
    assert api.handle({"action": "heartbeat", "host_id": "felix-3060", "token": tok})["code"] == 401
    assert m.main(["suspend", "--host-id", "felix-3060", "--reason", "test"], store=store) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "suspended"


def test_errors(cli, capsys):
    m, store = cli
    assert m.main(["approve", "--host-id", "nope"], store=store) == 1
    _reg(m, store, capsys)
    assert m.main(["approve", "--host-id", "felix-3060"], store=store) == 1  # pending: not approvable
    argv = [
        "register",
        "--host-id",
        "felix-3060",
        "--gpu-model",
        "X",
        "--gpu-count",
        "1",
        "--vram-gb",
        "1",
        "--owner-label",
        "x",
    ]
    assert m.main(argv, store=store) == 1  # duplicate
    argv[2] = "Bad ID!"
    assert m.main(argv, store=store) == 1


def test_flag_off_refuses(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("ROMA_HOSTS_ENABLED", raising=False)
    m = _cli()
    assert m.main(["list"], store=MemoryHostStore()) == 2
    assert m.main(["issue-token", "--host-id", "x", "--out", str(tmp_path / "t")], store=MemoryHostStore()) == 2
    assert not (tmp_path / "t").exists()
    assert "refused" in capsys.readouterr().err


def test_mask_dsn():
    m = _cli()
    masked = m.mask_dsn("postgresql://admin:s3cr3t-value@db.internal:5432/roma")
    assert "s3cr3t" not in masked and "admin" not in masked and "db.internal:5432/roma" in masked
    assert m.mask_dsn("") == "<unset>"


def _server(store):
    reg = hr.HostRegistry(store=store)
    reg.load_from_store()
    return HostApi(reg, store=store)


def _hb(api, hid, tok):
    return api.handle({"action": "heartbeat", "host_id": hid, "token": tok})


def _issue(m, store, hid, path):
    assert m.main(["issue-token", "--host-id", hid, "--out", str(path)], store=store) == 0
    return path.read_text().strip()


def test_rotation_old_401_new_200(cli, capsys, tmp_path):
    m, store = cli
    _reg(m, store, capsys)
    api = _server(store)
    old = _issue(m, store, "felix-3060", tmp_path / "t1")
    assert _hb(api, "felix-3060", old)["code"] == 200
    new = _issue(m, store, "felix-3060", tmp_path / "t2")
    assert _hb(api, "felix-3060", old)["code"] == 401
    assert _hb(api, "felix-3060", new)["code"] == 200


def test_token_of_host_a_rejected_for_host_b(cli, capsys, tmp_path):
    m, store = cli
    _reg(m, store, capsys, host_id="host-a")
    _reg(m, store, capsys, host_id="host-b")
    ta = _issue(m, store, "host-a", tmp_path / "a")
    _issue(m, store, "host-b", tmp_path / "b")
    api = _server(store)
    assert _hb(api, "host-a", ta)["code"] == 200
    assert _hb(api, "host-b", ta) == {"code": 401, "error": "unauthorized"}


def test_cli_suspend_seen_by_separate_registry(cli, capsys, tmp_path):
    m, store = cli
    _reg(m, store, capsys)
    tok = _issue(m, store, "felix-3060", tmp_path / "t")
    api = _server(store)
    assert m.main(["suspend", "--host-id", "felix-3060", "--reason", "ops"], store=store) == 0
    r = _hb(api, "felix-3060", tok)
    assert (r["code"], r["status"], r["heartbeat_accepted"]) == (200, "suspended", False)


def test_file_creation_failure_keeps_old_token(cli, capsys, tmp_path):
    m, store = cli
    _reg(m, store, capsys)
    old = _issue(m, store, "felix-3060", tmp_path / "t")
    capsys.readouterr()
    bad = tmp_path / "no-such-dir" / "t"
    assert m.main(["issue-token", "--host-id", "felix-3060", "--out", str(bad)], store=store) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "error: FileNotFoundError" in err
    assert not bad.exists()
    assert _hb(_server(store), "felix-3060", old)["code"] == 200


def test_write_failure_removes_file_and_restores_old_token(cli, capsys, tmp_path, monkeypatch):
    m, store = cli
    _reg(m, store, capsys)
    old = _issue(m, store, "felix-3060", tmp_path / "t")
    capsys.readouterr()

    def boom(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(m.os, "write", boom)
    out = tmp_path / "t2"
    assert m.main(["issue-token", "--host-id", "felix-3060", "--out", str(out)], store=store) == 1
    assert not out.exists() and "Traceback" not in capsys.readouterr().err
    assert _hb(_server(store), "felix-3060", old)["code"] == 200


def test_pg_load_host_empty_string_filters():
    from gpu_worker.host_store import PgHostStore

    seen = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args):
            seen.append((sql, args))

        def fetchall(self):
            return []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cur()

        def close(self):
            pass

    st = PgHostStore(dsn="", connect=lambda dsn: Conn())
    assert st.load_host("") is None
    assert "WHERE host_id=%s" in seen[-1][0] and seen[-1][1] == ("",)
    st.load_hosts()
    assert "WHERE" not in seen[-1][0]
