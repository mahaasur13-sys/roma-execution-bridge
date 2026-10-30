"""Host agent <-> HostApi end-to-end in-process (fake transport, CPU bench stub). No network."""

from __future__ import annotations

import logging

import pytest

from gpu_worker import host_bench
from gpu_worker import host_registry as hr
from gpu_worker.host_api import HostApi
from host_agent.agent import AgentConfig, HostAgent, load_config

TH = {"RTX_3060": {"min_tflops": 1e-9, "max_tflops": 1e9}}  # test-only bounds


def cpu_bench(seed, n, reps, device):
    return {"output_sha256": host_bench.reference_hash(seed, n), "elapsed_s": 0.5}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    reg = hr.HostRegistry()
    api = HostApi(reg)
    h = reg.apply(owner_contact_ref="r", gpu_model="RTX_3060", gpu_count=1, vram_gb=12, region="EU", own_host=True)
    tok = api.issue_host_token(h.host_id)
    return reg, api, h, tok


def _agent(api, h, tok, bench=cpu_bench, dry_run=False):
    return HostAgent(AgentConfig(url="fake://", host_id=h.host_id, token=tok), api.handle, bench=bench, dry_run=dry_run)


def test_end_to_end_verification(env):
    reg, api, h, tok = env
    agent = _agent(api, h, tok)
    assert agent.run_once()["stage"] == "idle"
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    out = agent.run_once()
    assert out["stage"] == "submitted" and out["code"] == 200
    assert (out["status"], out["trust_tier"]) == (hr.ACTIVE, "verified")
    assert api.handle({"action": "heartbeat", "host_id": h.host_id, "token": tok})["heartbeat_accepted"] is True


def test_wrong_hash_suspends(env):
    reg, api, h, tok = env
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    out = _agent(api, h, tok, bench=lambda *a: {"output_sha256": "0" * 64, "elapsed_s": 0.5}).run_once()
    assert out["status"] == hr.SUSPENDED


@pytest.mark.parametrize("token", ["wrong", "", None, 123])
def test_auth_failures(env, token):
    _, api, h, _ = env
    assert api.handle({"action": "heartbeat", "host_id": h.host_id, "token": token})["code"] == 401


def test_unknown_host_same_401(env):
    _, api, _, tok = env
    assert api.handle({"action": "heartbeat", "host_id": "host_nope", "token": tok}) == {
        "code": 401,
        "error": "unauthorized",
    }


def test_revoked_token(env):
    _, api, h, tok = env
    api.revoke_host_token(h.host_id)
    assert api.handle({"action": "heartbeat", "host_id": h.host_id, "token": tok})["code"] == 401


def test_job_token_single_use_and_replay(env):
    reg, api, h, tok = env
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    job = api.handle({"action": "fetch_job", "host_id": h.host_id, "token": tok})["job"]
    sub = {
        "action": "submit_result",
        "host_id": h.host_id,
        "token": tok,
        "job_id": job["job_id"],
        "job_token": job["job_token"],
        "output_sha256": host_bench.reference_hash(job["seed"], job["n"]),
        "elapsed_s": 0.5,
    }
    assert api.handle(dict(sub))["code"] == 200
    assert api.handle(dict(sub)) == {"code": 409, "error": "invalid_or_used_job_token"}


def test_refetch_invalidates_old_job_token_and_bad_token_consumes(env):
    reg, api, h, tok = env
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    j1 = api.handle({"action": "fetch_job", "host_id": h.host_id, "token": tok})["job"]
    j2 = api.handle({"action": "fetch_job", "host_id": h.host_id, "token": tok})["job"]
    base = {"action": "submit_result", "host_id": h.host_id, "token": tok, "job_id": j1["job_id"]}
    assert api.handle({**base, "job_token": j1["job_token"]})["code"] == 409  # superseded
    j3 = api.handle({"action": "fetch_job", "host_id": h.host_id, "token": tok})["job"]
    assert api.handle({**base, "job_token": "guess"})["code"] == 409
    assert api.handle({**base, "job_token": j3["job_token"]})["code"] == 409  # consumed by the guess
    assert j2["job_token"] != j3["job_token"]
    assert reg.get(h.host_id).status == hr.VERIFYING


def test_other_host_cannot_submit(env):
    reg, api, h, tok = env
    h2 = reg.apply(owner_contact_ref="r2", gpu_model="RTX_3060", gpu_count=1, vram_gb=12, region="EU")
    tok2 = api.issue_host_token(h2.host_id)
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    job = api.handle({"action": "fetch_job", "host_id": h.host_id, "token": tok})["job"]
    r = api.handle(
        {
            "action": "submit_result",
            "host_id": h2.host_id,
            "token": tok2,
            "job_id": job["job_id"],
            "job_token": job["job_token"],
            "output_sha256": "x",
            "elapsed_s": 1,
        }
    )
    assert r["code"] == 409


def test_flag_off_refuses(env, monkeypatch):
    _, api, h, tok = env
    monkeypatch.delenv("ROMA_HOSTS_ENABLED")
    assert api.handle({"action": "heartbeat", "host_id": h.host_id, "token": tok}) == {
        "code": 503,
        "error": "hosts_disabled",
    }
    with pytest.raises(hr.HostsDisabled):
        api.issue_host_token(h.host_id)
    out = _agent(api, h, tok).run_once()
    assert out == {"stage": "heartbeat", "code": 503, "error": "hosts_disabled"}


def test_rate_limit(env, monkeypatch):
    _, api, h, tok = env
    monkeypatch.setenv("ROMA_HOSTS_AGENT_RPM", "3")
    req = {"action": "heartbeat", "host_id": h.host_id, "token": tok}
    assert [api.handle(req)["code"] for _ in range(4)] == [200, 200, 200, 429]


def test_agent_refuses_non_verify_jobs(env):
    _, _, h, tok = env

    def transport(req):
        if req["action"] == "heartbeat":
            return {"code": 200, "status": "active"}
        return {"code": 200, "job": {"type": "workload", "job_id": "j", "command": "rm -rf /"}}

    ran = []
    agent = HostAgent(AgentConfig(url="x", host_id=h.host_id, token=tok), transport, bench=lambda *a: ran.append(a))
    assert agent.run_once() == {"stage": "refused", "job_type": "workload"}
    assert ran == []


def test_dry_run_does_not_bench_or_submit(env):
    reg, api, h, tok = env
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    ran = []
    out = _agent(api, h, tok, bench=lambda *a: ran.append(a), dry_run=True).run_once()
    assert out["stage"] == "dry_run" and ran == []
    assert reg.get(h.host_id).status == hr.VERIFYING


def test_token_never_logged_or_returned(env, caplog):
    reg, api, h, tok = env
    caplog.set_level(logging.DEBUG)
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    agent = _agent(api, h, tok)
    out = agent.run_once()
    api.handle({"action": "heartbeat", "host_id": h.host_id, "token": "bad-" + tok})
    blob = caplog.text + repr(out) + repr(agent.cfg) + str(agent.cfg)
    assert tok not in blob and "***" in repr(agent.cfg)


def test_load_config_from_token_file(tmp_path):
    tf = tmp_path / "tok"
    tf.write_text("file-token-value\n")
    cfgf = tmp_path / "agent.env"
    cfgf.write_text(f"# c\nROMA_URL=https://roma.invalid/agent\nROMA_HOST_ID=host_1\nROMA_HOST_TOKEN_FILE={tf}\n")
    cfg = load_config(str(cfgf), env={})
    assert cfg.token == "file-token-value" and cfg.host_id == "host_1"
    assert "file-token-value" not in repr(cfg)
    with pytest.raises(SystemExit):
        load_config(None, env={"ROMA_URL": "x"})
