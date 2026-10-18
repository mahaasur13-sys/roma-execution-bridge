"""R5 triage shadow-harness checks (hermetic — no network, no db).

Named ``jev_triage_checks.py`` (NOT ``test_*.py``) on purpose: the repo-wide canon
run (``.ci/run-completeness.json``, ``collected == 486``) must not shift. Run explicitly:

    .venv/bin/python -m pytest plugins/builtin/jev_triage_checks.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import plugins.builtin.jev_triage as shadow  # noqa: E402
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


def _choice_payload(choice="billing", prob=0.95, confidence=0.9):
    return {
        "model": "typesafe/jev-1.13",
        "answers": {
            "triage": {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: prob, "general": round(1 - prob, 4)},
                "confidence": confidence,
            }
        },
    }


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
    state = shadow.build_state("please refund my invoice")
    assert state == {"message_length": len("please refund my invoice")}
    assert "untrusted_message" not in state


def test_build_state_untrusted_prefixed(monkeypatch):
    monkeypatch.setattr(shadow, "_send_untrusted", lambda: True)
    state = shadow.build_state("my api key is sk_live_abc")
    assert state["untrusted_message"] == "UNTRUSTED:" + "my api key is sk_live_abc"


# ── Observation (fail-closed) ────────────────────────────────────────────


def test_observe_once_returns_observation():
    client = _FakeClient(payload=_choice_payload("billing", 0.95, 0.9))
    obs = shadow.observe_once("invoice", "billing", client=client)
    assert isinstance(obs, shadow.ShadowObservation)
    assert obs.choice == "billing"
    assert obs.heuristic_intent == "billing"
    assert obs.confidence == 0.9


def test_observe_once_fail_closed_on_jev_error():
    obs = shadow.observe_once("hi", "general", client=_FakeClient(error=JevError("timeout")))
    assert obs is None


def test_observe_once_fail_closed_on_bad_answer():
    obs = shadow.observe_once("hi", "general", client=_FakeClient(payload={"answers": {}}))
    assert obs is None


# ── shadow_triage: never blocks, never changes routing ───────────────────


def test_shadow_triage_disabled_no_thread(monkeypatch):
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    shadow.shadow_triage("hi", "general")
    assert scheduled == []


def test_shadow_triage_enabled_schedules(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    scheduled: list = []
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: scheduled.append((fn, args)))
    shadow.shadow_triage("hi", "general")
    assert len(scheduled) == 1
    assert scheduled[0][1] == ("hi", "general", None)


def test_shadow_triage_does_not_raise_when_observation_dropped(monkeypatch):
    monkeypatch.setenv(shadow._ENV_FLAG, "1")
    monkeypatch.setattr(shadow, "submit_observation", lambda fn, *args: False)  # переполнение/сбой
    shadow.shadow_triage("hi", "general")  # не должно бросить


# ── Minimal edit to _detect_intent: behavior preserved ───────────────────


def test_detect_intent_regression(monkeypatch):
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    monkeypatch.delenv(shadow._CASCADE_ENV_FLAG, raising=False)
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("run my job") == "job_submit"
    assert SupportChatPlugin._detect_intent("check job status") == "job_status"
    assert SupportChatPlugin._detect_intent("my invoice") == "billing"
    assert SupportChatPlugin._detect_intent("install plugin") == "plugin_help"
    assert SupportChatPlugin._detect_intent("the api threw an error") == "error_help"
    assert SupportChatPlugin._detect_intent("hello there") == "general"


# ── Cascade mode ─────────────────────────────────────────────────────────


def test_cascade_enabled_default_off(monkeypatch):
    monkeypatch.delenv(shadow._CASCADE_ENV_FLAG, raising=False)
    assert shadow.cascade_enabled() is False


def test_cascade_once_routes_when_confident():
    client = _FakeClient(payload=_choice_payload("billing", 0.95, 0.93))
    assert shadow.cascade_once("invoice", client=client) == "billing"


def test_cascade_once_none_below_threshold():
    client = _FakeClient(payload=_choice_payload("billing", 0.95, 0.85))
    assert shadow.cascade_once("invoice", client=client) is None


def test_cascade_once_fail_closed_on_error():
    assert shadow.cascade_once("hi", client=_FakeClient(error=JevError("timeout"))) is None


def test_detect_intent_cascade_routes_via_jev(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    monkeypatch.setattr(shadow, "cascade_once", lambda message, client=None, timeout=None: "billing")
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("whatever") == "billing"


def test_detect_intent_cascade_falls_back_to_heuristic(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    monkeypatch.delenv(shadow._ENV_FLAG, raising=False)
    monkeypatch.setattr(shadow, "cascade_once", lambda message, client=None, timeout=None: None)
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("my invoice") == "billing"

# ── cascade_once / _detect_intent: fail-closed (R5) ──────────────────────


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
def test_cascade_once_fail_closed_on_any_error(monkeypatch, error):
    assert shadow.cascade_once("invoice", client=_FakeClient(error=error)) is None


def test_cascade_once_fail_closed_on_model_pin_from_ctor(monkeypatch):
    """JevClient() падает на пине модели — маршрут остаётся на эвристике."""
    import core.jev_client as jev_client

    class _PinnedClient:
        def __init__(self, *args, **kwargs):
            raise JevError("model pin", reason="model_pin")

    monkeypatch.setattr(jev_client, "JevClient", _PinnedClient)
    assert shadow.cascade_once("invoice") is None


def test_cascade_once_fail_closed_when_normalize_answers_raises(monkeypatch):
    import core.jev_client as jev_client

    def _boom(payload):
        raise RuntimeError("bad payload")

    monkeypatch.setattr(jev_client, "normalize_answers", _boom)
    assert shadow.cascade_once("invoice", client=_FakeClient(payload={"answers": {}})) is None


def test_detect_intent_flag_off_never_calls_jev(monkeypatch):
    monkeypatch.delenv(shadow._CASCADE_ENV_FLAG, raising=False)
    calls: list = []
    monkeypatch.setattr(
        shadow, "cascade_once", lambda message, client=None, timeout=None: calls.append(1)
    )
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("my invoice") == "billing"
    assert calls == []


def test_detect_intent_accepts_known_category(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    monkeypatch.setattr(
        shadow, "cascade_once", lambda message, client=None, timeout=None: "job_status"
    )
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("my invoice") == "job_status"


def test_detect_intent_rejects_category_outside_intent_categories(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")
    monkeypatch.setattr(
        shadow, "cascade_once", lambda message, client=None, timeout=None: "not_a_category"
    )
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("my invoice") == "billing"


def test_detect_intent_fail_closed_when_cascade_raises(monkeypatch):
    monkeypatch.setenv(shadow._CASCADE_ENV_FLAG, "1")

    def _boom(message, client=None, timeout=None):
        raise JevError("network")

    monkeypatch.setattr(shadow, "cascade_once", _boom)
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("my invoice") == "billing"

# ── shadow-only путь: сбой shadow_triage не ломает _detect_intent ────────


def test_detect_intent_fail_closed_when_shadow_raises(monkeypatch):
    monkeypatch.delenv(shadow._CASCADE_ENV_FLAG, raising=False)

    def _boom(message, intent, client=None):
        raise RuntimeError("shadow down")

    monkeypatch.setattr(shadow, "shadow_triage", _boom)
    from plugins.builtin.support_chat import SupportChatPlugin

    assert SupportChatPlugin._detect_intent("my invoice") == "billing"
