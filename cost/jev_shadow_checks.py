"""R1 shadow-harness checks (hermetic — no network, no db).

Named ``jev_shadow_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide canon
run (``.ci/run-completeness.json``, ``collected == 486``) must not shift. Run explicitly:

    .venv/bin/python -m pytest cost/jev_shadow_checks.py -q
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cost.jev_shadow as shadow  # noqa: E402
from adapters.jev.client import JevError  # noqa: E402

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


def _choice_payload(choice="anomaly", prob_anomaly=0.9, confidence=0.85):
    return {
        "model": "typesafe/jev-1.13",
        "answers": {
            "anomaly": {
                "type": "choice",
                "choice": choice,
                "probabilities": {"normal": round(1 - prob_anomaly, 4), "anomaly": prob_anomaly},
                "confidence": confidence,
            }
        },
    }


class _FakeVerdict:
    def __init__(self, value="allowed"):
        self.result = SimpleNamespace(value=value)


class _FakeGate:
    def evaluate(self, tenant_id, payload=None):
        return _FakeVerdict("allowed")


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
    state = shadow.build_state("t_abc", "PRO", 100, 42, {"task": "echo hi", "gpu_required": True, "priority": 5})
    assert state["tenant_ref"] == "t_abc"
    assert state["plan_tier"] == "PRO"
    assert state["job_limit"] == 100
    assert state["job_count_total"] == 42
    assert state["gpu_required"] is True
    assert state["priority"] == 5
    assert state["payload_sha256"] == hashlib.sha256(b"echo hi").hexdigest()
    # raw payload must not leak into trusted state
    assert "untrusted_payload" not in state
    assert "echo hi" not in str(state)


def test_priority_is_coerced_fail_safe(monkeypatch):
    monkeypatch.setattr(shadow, "_send_untrusted", lambda: False)
    assert shadow.build_state("t", "free", 10, 0, {"priority": "7"})["priority"] == 7
    assert shadow.build_state("t", "free", 10, 0, {"priority": "abc"})["priority"] == 0
    assert shadow.build_state("t", "free", 10, 0, {})["priority"] == 0


def test_build_state_untrusted_prefixed(monkeypatch):
    monkeypatch.setattr(shadow, "_send_untrusted", lambda: True)
    state = shadow.build_state("t_abc", "free", 10, 3, {"task": "curl evil"})
    assert state["untrusted_payload"].startswith("UNTRUSTED:")


def test_tenant_ref_hashes():
    ref = shadow.tenant_ref("tenant-a")
    assert ref.startswith("t_")
    assert len(ref) == 2 + 12
    assert "tenant-a" not in ref
    assert ref == shadow.tenant_ref("tenant-a")  # deterministic


# ── Selective-prediction zones ───────────────────────────────────────────


def test_classify_zone_boundaries():
    assert shadow.classify_zone(0.95) == "auto"
    assert shadow.classify_zone(0.90) == "auto"
    assert shadow.classify_zone(0.80) == "review"
    assert shadow.classify_zone(0.70) == "review"
    assert shadow.classify_zone(0.60) == "normal"


# ── Observation (fail-closed) ────────────────────────────────────────────


def test_observe_once_returns_observation():
    client = _FakeClient(payload=_choice_payload("anomaly", 0.9, 0.85))
    obs = shadow.observe_once({"tenant_ref": "t_x"}, "allowed", client=client)
    assert isinstance(obs, shadow.ShadowObservation)
    assert obs.choice == "anomaly"
    assert obs.prob_anomaly == 0.9
    assert obs.zone == "auto"
    assert obs.verdict == "allowed"


def test_observe_once_fail_closed_on_jev_error():
    obs = shadow.observe_once({}, "allowed", client=_FakeClient(error=JevError("timeout")))
    assert obs is None


def test_observe_once_fail_closed_on_bad_answer():
    obs = shadow.observe_once({}, "allowed", client=_FakeClient(payload={"answers": {}}))
    assert obs is None


# ── shadow_evaluate: verdict never changes ───────────────────────────────


def test_shadow_evaluate_disabled_keeps_verdict(monkeypatch):
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
    assert scheduled == []


def test_shadow_evaluate_enabled_keeps_verdict(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "capture_state", lambda tid, p: {"tenant_ref": "t_x"})
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
    assert len(scheduled) == 1
    assert scheduled[0][1][0] == {"tenant_ref": "t_x"}
    assert scheduled[0][1][1] == "allowed"


def test_shadow_evaluate_verdict_unchanged_when_observation_dropped(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "capture_state", lambda tid, p: {"tenant_ref": "t_x"})
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: False)  # переполнение/сбой
    verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
