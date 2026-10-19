"""R1 shadow-harness checks (hermetic — no network, no db).

Named ``jev_shadow_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide canon
run (``.ci/run-completeness.json``, ``collected == 486``) must not shift. Run explicitly:

    .venv/bin/python -m pytest cost/jev_shadow_checks.py -q
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cost.jev_shadow as shadow  # noqa: E402
from adapters.jev.client import JevDisabled, JevError, JevRateLimited, JevTimeout  # noqa: E402

import pytest  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402


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
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args, **kwargs: scheduled.append((fn, args)))
    verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
    assert scheduled == []


def test_shadow_evaluate_enabled_keeps_verdict(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "capture_state", lambda tid, p: {"tenant_ref": "t_x"})
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args, **kwargs: scheduled.append((fn, args)))
    verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
    assert len(scheduled) == 1
    assert scheduled[0][1][0] == {"tenant_ref": "t_x"}
    assert scheduled[0][1][1] == "allowed"


def test_shadow_evaluate_verdict_unchanged_when_observation_dropped(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "capture_state", lambda tid, p: {"tenant_ref": "t_x"})
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args, **kwargs: False)  # переполнение/сбой
    verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"


# ── Metrics (R1): дефолтный registry, сравнение дельт ────────────────────


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class _Delta:
    """Дельта метрики до/после блока: значения живут в общем registry между тестами."""

    def __init__(self, name: str, **labels: str) -> None:
        self._name = name
        self._labels = labels

    def __enter__(self) -> "_Delta":
        self._before = _sample(self._name, **self._labels)
        return self

    def __exit__(self, *exc: object) -> bool:
        self._after = _sample(self._name, **self._labels)
        return False

    @property
    def value(self) -> float:
        return self._after - self._before


def _records() -> list[dict]:
    """Записи shadow-JSONL текущего теста (путь задаёт autouse-фикстура)."""
    path = Path(os.environ["JEV_SHADOW_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_flag_off_no_metrics_and_no_jev_call(monkeypatch):
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    calls: list = []
    monkeypatch.setattr("core.jev_client.JevClient", lambda *a, **k: calls.append(1))
    with _Delta("roma_jev_shadow_observations_total", point="R1", outcome="ok") as obs_d, _Delta(
        "roma_jev_shadow_fallback_total", point="R1", reason="jev_error"
    ) as fb_d, _Delta("roma_jev_shadow_latency_seconds_count", point="R1") as lat_d:
        verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
    assert calls == []  # Jev не вызывался
    assert (obs_d.value, fb_d.value, lat_d.value) == (0.0, 0.0, 0.0)


def test_ok_observation_increments_counters_and_latency():
    client = _FakeClient(payload=_choice_payload("anomaly", 0.9, 0.85))
    with _Delta("roma_jev_shadow_observations_total", point="R1", outcome="ok") as obs_d, _Delta(
        "roma_jev_shadow_agreement_total", point="R1", agree="no"
    ) as agr_d, _Delta("roma_jev_shadow_latency_seconds_count", point="R1") as lat_d:
        obs = shadow.observe_once({"tenant_ref": "t_x"}, "allowed", client=client)
    assert obs is not None
    assert (obs_d.value, agr_d.value, lat_d.value) == (1.0, 1.0, 1.0)


def test_ok_observation_agreement_yes():
    client = _FakeClient(payload=_choice_payload("normal", 0.1, 0.85))
    with _Delta("roma_jev_shadow_agreement_total", point="R1", agree="yes") as d:
        obs = shadow.observe_once({}, "allowed", client=client)
    assert obs is not None
    assert d.value == 1.0


def test_timeout_increments_fallback_and_observations():
    with _Delta("roma_jev_shadow_fallback_total", point="R1", reason="timeout") as fb_d, _Delta(
        "roma_jev_shadow_observations_total", point="R1", outcome="fallback"
    ) as obs_d:
        obs = shadow.observe_once({}, "allowed", client=_FakeClient(error=JevTimeout("slow")))
    assert obs is None
    assert (fb_d.value, obs_d.value) == (1.0, 1.0)


def test_rate_limited_and_generic_error_reasons():
    with _Delta("roma_jev_shadow_fallback_total", point="R1", reason="rate_limited") as rl, _Delta(
        "roma_jev_shadow_fallback_total", point="R1", reason="jev_error"
    ) as je:
        shadow.observe_once({}, "allowed", client=_FakeClient(error=JevRateLimited("429")))
        shadow.observe_once({}, "allowed", client=_FakeClient(error=JevError("boom")))
    assert (rl.value, je.value) == (1.0, 1.0)

def test_disabled_has_own_reason():
    with _Delta("roma_jev_shadow_fallback_total", point="R1", reason="disabled") as dis, _Delta(
        "roma_jev_shadow_fallback_total", point="R1", reason="jev_error"
    ) as je:
        obs = shadow.observe_once({}, "allowed", client=_FakeClient(error=JevDisabled("flag off")))
    assert obs is None
    assert (dis.value, je.value) == (1.0, 0.0)
    assert _records()[0]["reason"] == "disabled"


def test_no_answer_increments_fallback():
    with _Delta("roma_jev_shadow_fallback_total", point="R1", reason="no_answer") as d:
        obs = shadow.observe_once({}, "allowed", client=_FakeClient(payload={"answers": {}}))
    assert obs is None
    assert d.value == 1.0


def test_state_capture_failure_increments_fallback(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")

    def _boom(tid, payload):
        raise RuntimeError("no state")

    monkeypatch.setattr(shadow, "capture_state", _boom)
    with _Delta("roma_jev_shadow_fallback_total", point="R1", reason="state_capture") as d:
        verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
    assert d.value == 1.0


# ── JSONL schema=1 ───────────────────────────────────────────────────────


def test_ok_record_carries_schema_fields():
    shadow.observe_once({"tenant_ref": "t_x"}, "allowed", client=_FakeClient(payload=_choice_payload()))
    rec = _records()[0]
    assert rec["point"] == "R1"
    assert rec["schema"] == 1
    assert rec["outcome"] == "ok"
    assert rec["reason"] == ""
    assert isinstance(rec["latency_ms"], float) and rec["latency_ms"] >= 0.0
    # старые поля не удалены
    assert rec["choice"] == "anomaly"
    assert "verdict" in rec and "observed_at" in rec and "prob_anomaly" in rec


def test_fallback_record_carries_schema_fields():
    shadow.observe_once({}, "allowed", client=_FakeClient(error=JevTimeout("slow")))
    rec = _records()[0]
    assert rec["schema"] == 1
    assert rec["outcome"] == "fallback"
    assert rec["reason"] == "timeout"
    assert rec["verdict"] == "allowed"
    assert rec["latency_ms"] >= 0.0


# ── Телеметрия не влияет на вердикт ──────────────────────────────────────


def test_metric_failure_never_changes_verdict(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("registry down")

    monkeypatch.setattr(shadow, "track_jev_shadow", _boom)
    obs = shadow.observe_once({}, "allowed", client=_FakeClient(payload=_choice_payload()))
    assert obs is not None  # метрика упала, наблюдение всё равно записано

    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "capture_state", lambda tid, p: {"tenant_ref": "t_x"})
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *a, **kw: scheduled.append(1))
    verdict = shadow.shadow_evaluate(_FakeGate(), "tenant-a", {})
    assert verdict.result.value == "allowed"
    assert len(scheduled) == 1
