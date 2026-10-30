"""With ROMA_HOSTS_ENABLED / ROMA_PREFER_OWN_HOSTS / ROMA_RUNPOD_ENABLED unset the
dispatcher must behave exactly like the frozen 066c2ba copy."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

import backends.dispatcher as new
from backends.local import LocalBackend

FLAGS = ("ROMA_HOSTS_ENABLED", "ROMA_PREFER_OWN_HOSTS", "ROMA_RUNPOD_ENABLED")


def _old():
    p = Path(__file__).resolve().parent / "fixtures" / "dispatcher_066c2ba.py"
    spec = importlib.util.spec_from_file_location("dispatcher_066c2ba", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def clean(monkeypatch):
    for f in FLAGS + ("VAST_KEY", "VASTAI_API_KEY", "RUNPOD_API_KEY", "ROMA_EXECUTION_BACKEND"):
        monkeypatch.delenv(f, raising=False)
    new._backend, new._backend_name = None, ""
    yield monkeypatch
    new._backend, new._backend_name = None, ""


class _Rec(LocalBackend):
    seen: list = []

    async def dispatch(self, ctx):
        _Rec.seen.append(ctx)
        return {"status": "queued", "job_id": ctx.job_id}


@pytest.mark.parametrize("name", [None, "local", "vastai", "runpod", "nonsense", ""])
def test_get_backend_identical_when_flags_off(clean, name):
    old = _old()
    a, b = old.get_backend(name), new.get_backend(name)
    assert type(a) is type(b) and old._backend_name == new._backend_name


def test_list_backends_identical_when_flags_off(clean):
    old = _old()
    assert old.list_backends() == new.list_backends()


def test_dispatch_job_identical_when_flags_off(clean):
    old = _old()
    payload = {"backend": "local", "image": "img", "priority": 3, "min_vram_gb": 24}
    outs = []
    for mod in (old, new):
        _Rec.seen = []
        clean.setattr(mod, "get_backend", lambda name=None: _Rec())
        outs.append((asyncio.run(mod.dispatch_job("j1", "t1", payload)), repr(_Rec.seen[0])))
    assert outs[0] == outs[1]


def test_runpod_flag_on_is_stub_and_fail_closed(clean):
    clean.setenv("ROMA_RUNPOD_ENABLED", "1")
    assert isinstance(new.get_backend("runpod"), LocalBackend)  # no key -> local fallback
    new._backend = None
    clean.setenv("RUNPOD_API_KEY", "dummy-not-a-secret")
    b = new.get_backend("runpod")
    assert b.backend_name == "runpod"
    assert asyncio.run(b.dispatch(new.JobContext(job_id="j", tenant_id="t")))["status"] == "not_implemented"
    assert new.list_backends()["runpod"]["stub"] is True


def test_preference_annotates_but_does_not_reroute(clean):
    clean.setenv("ROMA_HOSTS_ENABLED", "1")
    clean.setenv("ROMA_PREFER_OWN_HOSTS", "1")
    clean.setattr(new, "get_backend", lambda name=None: _Rec())
    clean.setattr(new, "_host_registry", None)
    r = asyncio.run(new.dispatch_job("j2", "t", {}))
    assert r["status"] == "queued"
    assert r["routing_preference"] == ["vastai", "runpod"]  # no verified hosts yet
    assert r["host_execution"] == "not_implemented"
