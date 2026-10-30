"""/hosts/agent/* over FastAPI TestClient (in-memory store) and the agent's HTTP transport."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gpu_worker import host_bench
from gpu_worker import host_registry as hr
from gpu_worker.host_api import HostApi
from gpu_worker.host_store import MemoryHostStore, store_from_env
from host_agent.agent import AgentConfig, HostAgent, http_transport
from routers import hosts_agent

TH = {"RTX_3060": {"min_tflops": 1e-9, "max_tflops": 1e9}}  # test-only bounds


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_ENABLED", "1")
    monkeypatch.delenv("ROMA_HOSTS_STORE", raising=False)
    store = MemoryHostStore()
    reg = hr.HostRegistry(store=store)
    api = HostApi(reg, store=store)
    hosts_agent.set_host_api(api)
    monkeypatch.setattr(hosts_agent, "ip_limiter", hosts_agent.IpRateLimiter())
    app = FastAPI()
    app.include_router(hosts_agent.router)
    h = reg.apply(owner_contact_ref="r", gpu_model="RTX_3060", gpu_count=1, vram_gb=12, region="EU", own_host=True)
    tok = api.issue_host_token(h.host_id)
    yield TestClient(app), reg, h, tok
    hosts_agent.set_host_api(None)


def _hdr(h, tok):
    return {"Authorization": f"Bearer {tok}", "X-Roma-Host-Id": h.host_id}


def test_http_flow(setup):
    c, reg, h, tok = setup
    assert c.post("/hosts/agent/heartbeat", headers=_hdr(h, tok)).json()["status"] == "pending"
    assert c.post("/hosts/agent/job", headers=_hdr(h, tok)).status_code == 204
    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    job = c.post("/hosts/agent/job", headers=_hdr(h, tok)).json()["job"]
    body = {
        "job_id": job["job_id"],
        "job_token": job["job_token"],
        "output_sha256": host_bench.reference_hash(job["seed"], job["n"]),
        "elapsed_s": 0.5,
    }
    r = c.post("/hosts/agent/result", headers=_hdr(h, tok), json=body)
    assert r.status_code == 200 and r.json() == {"status": "active", "trust_tier": "verified"}
    assert c.post("/hosts/agent/result", headers=_hdr(h, tok), json=body).status_code == 409


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}])
def test_http_auth_failures(setup, headers):
    c, _, h, _ = setup
    r = c.post("/hosts/agent/heartbeat", headers={"X-Roma-Host-Id": h.host_id, **headers})
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


def test_http_bad_body(setup):
    c, _, h, tok = setup
    r = c.post("/hosts/agent/result", headers={**_hdr(h, tok), "Content-Type": "application/json"}, content=b"[1]")
    assert r.status_code == 400


def test_http_runtime_flag_off_503(setup, monkeypatch):
    c, _, h, tok = setup
    monkeypatch.delenv("ROMA_HOSTS_ENABLED")
    assert c.post("/hosts/agent/heartbeat", headers=_hdr(h, tok)).status_code == 503


def test_http_internal_error_fails_closed(setup, monkeypatch):
    c, _, h, tok = setup

    class Boom:
        def handle(self, req):
            raise RuntimeError("db down " + req["token"])

    hosts_agent.set_host_api(Boom())
    r = c.post("/hosts/agent/heartbeat", headers=_hdr(h, tok))
    assert r.status_code == 503 and tok not in r.text


def test_agent_over_http_transport_e2e(setup, caplog):
    c, reg, h, tok = setup
    caplog.set_level(logging.DEBUG)

    def post(url, headers, data, timeout):
        r = c.post(url, headers=headers, content=data)
        return r.status_code, r.content

    reg.start_verification(h.host_id, thresholds=TH, n=8, reps=1)
    agent = HostAgent(
        AgentConfig(url="http://testserver/hosts/agent", host_id=h.host_id, token=tok),
        http_transport("http://testserver/hosts/agent", post=post),
        bench=lambda seed, n, reps, dev: {"output_sha256": host_bench.reference_hash(seed, n), "elapsed_s": 0.5},
    )
    out = agent.run_once()
    assert out["stage"] == "submitted" and out["status"] == "active"
    assert tok not in caplog.text


def test_routes_not_registered_when_flag_off(monkeypatch):
    monkeypatch.delenv("ROMA_HOSTS_ENABLED", raising=False)
    import main

    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert not any(p.startswith("/hosts/agent") for p in paths)


def test_store_from_env_defaults_to_memory(monkeypatch):
    monkeypatch.delenv("ROMA_HOSTS_STORE", raising=False)
    assert isinstance(store_from_env(), MemoryHostStore)


def test_ip_rate_limit_runs_before_auth(setup, monkeypatch):
    c, _, h, tok = setup
    monkeypatch.setenv("ROMA_HOSTS_AGENT_IP_RPM", "2")
    calls = []

    class Counting:
        def handle(self, req):
            calls.append(req["action"])
            return {"code": 401, "error": "unauthorized"}

    hosts_agent.set_host_api(Counting())
    codes = [c.post("/hosts/agent/heartbeat", headers={"Authorization": "Bearer bad"}).status_code for _ in range(4)]
    assert codes == [401, 401, 429, 429]
    assert len(calls) == 2  # throttled requests never reached token verification


def test_ip_limiter_window_and_key_cap(monkeypatch):
    monkeypatch.setenv("ROMA_HOSTS_AGENT_IP_RPM", "1")
    now = [0.0]
    lim = hosts_agent.IpRateLimiter(clock=lambda: now[0], max_keys=2)
    assert lim.allow("a") and not lim.allow("a") and lim.allow("b")
    assert not lim.allow("c")  # key table full, entries still fresh: fail closed
    now[0] = 61.0
    assert lim.allow("a") and lim.allow("c")  # window passed; stale keys evicted
