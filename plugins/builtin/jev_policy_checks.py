"""R3 policy shadow-harness checks (hermetic — no network, no db).

Named ``jev_policy_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide canon
run (``.ci/run-completeness.json``, ``collected == 486``) must not shift. Run explicitly:

    .venv/bin/python -m pytest plugins/builtin/jev_policy_checks.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import plugins.builtin.jev_policy as shadow  # noqa: E402
from adapters.jev.client import JevError, JevTimeout  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_shadow_log(tmp_path, monkeypatch):
    """Не даём тестам писать в реальный logs/jev_shadow.jsonl."""
    monkeypatch.setenv("JEV_SHADOW_LOG", str(tmp_path / "shadow.jsonl"))


# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error
        self.calls: list[tuple] = []

    def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if self._error is not None:
            raise self._error
        return self._payload or {}


def _choice_payload(choice="violation", prob=0.95, confidence=0.9):
    return {
        "model": "typesafe/jev-1.13",
        "answers": {
            "policy": {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: prob, "compliant": round(1 - prob, 4)},
                "confidence": confidence,
            }
        },
    }


class _FakePlugin:
    def __init__(self, action="allow"):
        self._action = action

    async def run(self, tenant_id, payload, tenant, *, trace_id=""):
        return SimpleNamespace(action=self._action, reason="ok", confidence=1.0, metadata={})


# ── Flag ─────────────────────────────────────────────────────────────────


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    assert shadow.is_enabled() is False


def test_is_enabled_on(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    assert shadow.is_enabled() is True


# ── Trusted / untrusted split ────────────────────────────────────────────


def test_build_state_trusted_only(monkeypatch):
    monkeypatch.setattr(shadow, "_send_untrusted", lambda: False)
    state = shadow.build_state(
        "t_abc",
        {"plan": "pro", "job_limit": 50, "active_jobs": 10},
        {"estimated_cost": 42.5, "task": "echo hi"},
    )
    assert state["tenant_ref"] == "t_abc"
    assert state["plan"] == "pro"
    assert state["job_limit"] == 50
    assert state["active_jobs"] == 10
    assert state["estimated_cost"] == 42.5
    assert state["payload_sha256"] == hashlib.sha256(b"echo hi").hexdigest()
    assert "untrusted_payload" not in state
    assert "echo hi" not in str(state)


def test_build_state_untrusted_prefixed(monkeypatch):
    monkeypatch.setattr(shadow, "_send_untrusted", lambda: True)
    state = shadow.build_state("t_abc", {"plan": "free"}, {"task": "curl evil"})
    assert state["untrusted_payload"].startswith("UNTRUSTED:")


# ── Observation (fail-closed) ────────────────────────────────────────────


def test_observe_once_returns_observation():
    client = _FakeClient(payload=_choice_payload("violation", 0.95, 0.9))
    obs = shadow.observe_once({"tenant_ref": "t_x"}, "deny", client=client)
    assert isinstance(obs, shadow.ShadowObservation)
    assert obs.choice == "violation"
    assert obs.verdict == "deny"
    assert obs.confidence == 0.9


def test_observe_once_fail_closed_on_jev_error():
    obs = shadow.observe_once({}, "allow", client=_FakeClient(error=JevError("timeout")))
    assert obs is None


def test_observe_once_fail_closed_on_bad_answer():
    obs = shadow.observe_once({}, "allow", client=_FakeClient(payload={"answers": {}}))
    assert obs is None


# ── shadow_run: verdict never changes ────────────────────────────────────


def test_shadow_run_disabled_keeps_verdict(monkeypatch):
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    result = asyncio.run(shadow.shadow_run(_FakePlugin(), "t1", {}, {"plan": "free"}))
    assert result.action == "allow"
    assert scheduled == []


def test_shadow_run_enabled_keeps_verdict(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    result = asyncio.run(shadow.shadow_run(_FakePlugin(), "t1", {}, {"plan": "free"}))
    assert result.action == "allow"
    assert len(scheduled) == 1
    assert scheduled[0][1][1] == "allow"


def test_shadow_run_verdict_unchanged_when_observation_dropped(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: False)  # переполнение/сбой
    result = asyncio.run(shadow.shadow_run(_FakePlugin(), "t1", {}, {"plan": "free"}))
    assert result.action == "allow"


# ── Cascade mode (escalate-only, never downgrade) ───────────────────────


def test_cascade_enabled_default_off(monkeypatch):
    monkeypatch.delenv(shadow._CASCADE_ENV_FLAG, raising=False)
    assert shadow.cascade_enabled() is False


def test_cascade_once_routes_when_confident():
    client = _FakeClient(payload=_choice_payload("violation", 0.95, 0.9))
    assert shadow.cascade_once({"tenant_ref": "t_x"}, client=client) == "violation"


def test_cascade_once_none_below_threshold():
    client = _FakeClient(payload=_choice_payload("violation", 0.95, 0.80))
    assert shadow.cascade_once({"tenant_ref": "t_x"}, client=client) is None


def test_cascade_once_fail_closed_on_error():
    assert shadow.cascade_once({}, client=_FakeClient(error=JevError("timeout"))) is None


def test_cascade_run_escalates_allow_when_jev_confident(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "cascade_once", lambda state, client=None, timeout=None: "violation")
    result = asyncio.run(shadow.cascade_run(_FakePlugin("allow"), "t1", {}, {"plan": "free"}))
    assert str(result.action) == "escalate"


def test_cascade_run_never_downgrades_deny(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "cascade_once", lambda state, client=None, timeout=None: "compliant")
    result = asyncio.run(shadow.cascade_run(_FakePlugin("deny"), "t1", {}, {"plan": "free"}))
    assert str(result.action) == "deny"


def test_cascade_run_keeps_allow_when_not_confident(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "cascade_once", lambda state, client=None, timeout=None: None)
    result = asyncio.run(shadow.cascade_run(_FakePlugin("allow"), "t1", {}, {"plan": "free"}))
    assert str(result.action) == "allow"

# ── cascade_run: fail-closed + escalate-only (любая ошибка Jev → baseline) ─


def _run_cascade(monkeypatch, *, action="allow", client=None, escalate=None):
    """cascade_run с включённым флагом; baseline = детерминированный результат."""
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    if escalate is not None:
        monkeypatch.setattr(shadow, "_escalate_for_review", escalate)
    return asyncio.run(
        shadow.cascade_run(_FakePlugin(action), "t1", {}, {"plan": "free"}, client=client)
    )


def test_cascade_run_flag_off_never_calls_jev(monkeypatch):
    monkeypatch.delenv(shadow._CASCADE_ENV_FLAG, raising=False)
    client = _FakeClient(payload=_choice_payload("violation", 0.99, 0.99))
    result = asyncio.run(
        shadow.cascade_run(_FakePlugin("allow"), "t1", {}, {"plan": "free"}, client=client)
    )
    assert str(result.action) == "allow"
    assert client.calls == []  # при выключенном каскаде Jev не вызывается


@pytest.mark.parametrize(
    "error",
    [
        JevError("network"),
        JevTimeout("slow"),
        JevError("pin", reason="model_pin"),
        JevError("pin response", reason="model_pin_response"),
        RuntimeError("kaboom"),
    ],
)
def test_cascade_run_fail_closed_on_any_error(monkeypatch, error):
    result = _run_cascade(monkeypatch, client=_FakeClient(error=error))
    assert str(result.action) == "allow"  # baseline; исключение не выходит наружу


def test_cascade_run_fail_closed_on_model_pin_from_ctor(monkeypatch):
    """JevClient() падает на пине модели — вердикт остаётся детерминированным."""
    import core.jev_client as jev_client

    class _PinnedClient:
        def __init__(self, *args, **kwargs):
            raise JevError("model pin", reason="model_pin")

    monkeypatch.setattr(jev_client, "JevClient", _PinnedClient)
    result = _run_cascade(monkeypatch, client=None)
    assert str(result.action) == "allow"


def test_cascade_run_fail_closed_when_normalize_answers_raises(monkeypatch):
    import core.jev_client as jev_client

    def _boom(payload):
        raise RuntimeError("bad payload")

    monkeypatch.setattr(jev_client, "normalize_answers", _boom)
    result = _run_cascade(monkeypatch, client=_FakeClient(payload={"answers": {}}))
    assert str(result.action) == "allow"


def test_cascade_run_fail_closed_when_escalate_raises(monkeypatch):
    """Исключение внутри _escalate_for_review не доходит до запроса."""

    def _boom(*args, **kwargs):
        raise RuntimeError("policy_engine unavailable")

    result = _run_cascade(
        monkeypatch,
        client=_FakeClient(payload=_choice_payload("violation", 0.99, 0.99)),
        escalate=_boom,
    )
    assert str(result.action) == "allow"


def test_cascade_run_escalate_only_never_downgrades_deny(monkeypatch):
    client = _FakeClient(payload=_choice_payload("violation", 0.99, 0.99))
    result = _run_cascade(monkeypatch, action="deny", client=client)
    assert str(result.action) == "deny"

# ── shadow_policy: non-dict / None evaluate_policies result ───────────────


def _patch_policy_engine(monkeypatch, evaluate_policies):
    """Подменить policy_engine.evaluate_policies на герметичный fake (без db/tier_profiles)."""
    fake = ModuleType("policy_engine")
    fake.evaluate_policies = evaluate_policies
    monkeypatch.setitem(sys.modules, "policy_engine", fake)


def test_shadow_policy_enabled_keeps_verdict(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    _patch_policy_engine(
        monkeypatch,
        lambda tenant_id, action, context=None: {"result": "allowed", "reason": "ok", "policy_name": "quota_policy"},
    )
    result = shadow.shadow_policy("t1", "decision.create", {"estimated_cost": 1.0})
    assert result == {"result": "allowed", "reason": "ok", "policy_name": "quota_policy"}
    assert len(scheduled) == 1


def test_shadow_policy_none_result_kept(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    _patch_policy_engine(monkeypatch, lambda tenant_id, action, context=None: None)
    result = shadow.shadow_policy("t1", "decision.create", {})
    assert result is None  # вердикт не меняется
    assert scheduled == []  # наблюдение пропущено, ничего не брошено


def test_shadow_policy_nondict_result_kept(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    _patch_policy_engine(monkeypatch, lambda tenant_id, action, context=None: ["allowed", "ok"])
    result = shadow.shadow_policy("t1", "decision.create", {})
    assert result == ["allowed", "ok"]  # вердикт не меняется
    assert scheduled == []  # наблюдение пропущено, ничего не брошено


def test_shadow_policy_disabled_noop(monkeypatch):
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    _patch_policy_engine(monkeypatch, lambda tenant_id, action, context=None: None)
    result = shadow.shadow_policy("t1", "decision.create", {})
    assert result is None  # при выключенном флаге вердикт возвращается как есть
    assert scheduled == []
