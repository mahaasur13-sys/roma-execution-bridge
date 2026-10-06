"""Проверки sidecar-адаптера Jev: сеть замокана, живой ключ не используется.

Почему имя файла не `test_*.py`: repo-wide прогон ядра (`python -m pytest -q`,
target=`.`) обязан остаться на каноне `.ci/run-completeness.json`
(`collected == 486`). Файл с именем `test_*.py` попал бы в этот прогон и сдвинул
канон, а существующие тесты ядра менять нельзя. Набор запускается явно:

    .venv/bin/python -m pytest adapters/jev/checks/jev_checks.py -q

Сеть: наружу не уходит ни один запрос — транспорт подменяется `_transport()`,
`typesafe_sdk` подменяется стабом через `sys.modules`. Живой ключ не нужен.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters.jev.adapter import JevAdapter, _risk_value  # noqa: E402
from adapters.jev.client import JevClient, JevRateLimited, JevTimeout  # noqa: E402
from adapters.jev.contract import THRESHOLDS, state_bytes  # noqa: E402

FAKE_KEY = "unit-test-placeholder-key"


def _env(**overrides: str) -> dict[str, str]:
    base = {"JEV_ENABLED": "1", "TYPESAFE_API_KEY": FAKE_KEY}
    base.update(overrides)
    return base


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "act": "execute-job",
        "job_id": "job-1",
        "tenant_ref": "tenant-a",
        "plan_tier": "PRO",
        "queue": "default",
        "region": "eu-1",
        "gpu_type": "A100",
        "priority": 5,
        "requested_seconds": 600,
        "worker_availability": "3/4",
        "retry_count": 0,
        "last_failure_class": "",
    }
    state.update(overrides)
    return state


def _payload(gate: float, choice: str, run_probability: float, risk: int, confidence: float) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "gate": {"type": "noul", "noul": gate},
            "next": {
                "type": "choice",
                "choice": choice,
                "probabilities": {"run": run_probability, "ask": 0.5, "abort": 0.05},
                "confidence": confidence,
            },
            "risk": {"type": "score", "score": risk, "probabilities": {str(risk): 0.7}, "confidence": confidence},
        },
    }


def _risk_payload(risk_value: Any, gate: float = 0.99, choice: str = "run", run_probability: float = 0.99, confidence: float = 0.99) -> dict[str, Any]:
    """Ответ Jev, где все прочие сигналы на уровне execute: решение задаёт только risk."""
    payload = _payload(gate, choice, run_probability, 1, confidence)
    payload["answers"]["risk"]["score"] = risk_value
    return payload


class _Recorder:
    """Транспорт-двойник: считает вызовы и запоминает payload (без сети)."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.keys: list[str] = []
        self._result = result
        self._error = error

    def __call__(self, url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
        self.calls.append(payload)
        self.urls.append(url)
        self.keys.append(key)
        if self._error is not None:
            raise self._error
        return self._result or {}


def _adapter(recorder: _Recorder, env: dict[str, str] | None = None) -> JevAdapter:
    client = JevClient(env=env or _env(), transport=recorder, sleep=lambda _seconds: None)
    return JevAdapter(client=client)


# -- гейт включения ---------------------------------------------------------


def test_flag_off_is_noop_without_network() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 1, 0.99))
    verdict = _adapter(recorder, env=_env(JEV_ENABLED="0")).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "disabled"
    assert recorder.calls == []


def test_missing_key_is_noop_without_network() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 1, 0.99))
    env = {"JEV_ENABLED": "1", "TYPESAFE_API_KEY": ""}
    verdict = _adapter(recorder, env=env).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "disabled"
    assert recorder.calls == []


# -- отказы транспорта ------------------------------------------------------


def test_timeout_maps_to_skip() -> None:
    recorder = _Recorder(error=JevTimeout("timeout: <redacted>"))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "timeout"
    assert len(recorder.calls) == 1


def test_rate_limited_exhausts_one_retry_then_skips() -> None:
    recorder = _Recorder(error=JevRateLimited("HTTP 429"))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "rate_limited"
    assert len(recorder.calls) == 2


def test_transport_receives_official_endpoint_and_env_key() -> None:
    recorder = _Recorder(result=_payload(0.95, "run", 0.9, 2, 0.9))
    _adapter(recorder).decide(_state())
    assert recorder.urls == ["https://api.typesafe.ai/v1/systemone"]
    assert recorder.keys == [FAKE_KEY]


# -- уверенность и форма ответа --------------------------------------------


def test_low_confidence_is_skip() -> None:
    recorder = _Recorder(result=_payload(0.95, "run", 0.9, 2, THRESHOLDS["confidence_floor"] - 0.3))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "low_confidence"


def test_missing_choice_answer_is_skip() -> None:
    payload = _payload(0.95, "run", 0.9, 2, 0.9)
    del payload["answers"]["next"]
    verdict = _adapter(_Recorder(result=payload)).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "bad_answer"


def test_non_json_payload_is_skip() -> None:
    verdict = _adapter(_Recorder(result={"answers": "не объект"})).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "bad_answer"


# -- пороги: execute / confirm / abort -------------------------------------


def test_valid_choice_executes_with_single_call() -> None:
    recorder = _Recorder(result=_payload(0.91, "run", 0.88, 2, 0.82))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "execute"
    assert verdict["reason"] == "gate_and_choice"
    assert len(recorder.calls) == 1
    assert sorted(recorder.calls[0]["questions"]) == ["gate", "next", "risk"]
    assert recorder.calls[0]["model"] == "jev-1.13.0"
    assert verdict["evidence"]["gate_noul"] == 0.91


def test_gate_below_floor_aborts() -> None:
    recorder = _Recorder(result=_payload(0.10, "abort", 0.02, 1, 0.8))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "abort"
    assert verdict["reason"] == "gate_below_floor"


def test_ask_choice_confirms() -> None:
    recorder = _Recorder(result=_payload(0.72, "ask", 0.3, 2, 0.8))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "confirm"
    assert verdict["reason"] == "insufficient_signal"


def test_high_risk_never_executes() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 5, 0.99))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "confirm"
    assert verdict["reason"] == "risk_above_execute"


# -- запреты и лимиты -------------------------------------------------------


def test_forbidden_domain_billing_is_skip_without_call() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 1, 0.99))
    verdict = _adapter(recorder).decide(_state(), domain="billing-activate")
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "forbidden_domain"
    assert recorder.calls == []


def test_forbidden_domain_from_act_field_is_skip() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 1, 0.99))
    verdict = _adapter(recorder).decide(_state(act="quota-adjust"))
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "forbidden_domain"
    assert verdict["evidence"]["domain"] == "quota-adjust"
    assert recorder.calls == []


def test_oversized_field_is_skip_without_truncation() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 1, 0.99))
    verdict = _adapter(recorder).decide(_state(last_failure_class="x" * 60000))
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "state_too_large"
    assert verdict["evidence"]["limit"] == "field"
    assert verdict["evidence"]["field"] == "last_failure_class"
    assert recorder.calls == []


def test_state_over_byte_limit_is_skip_without_call() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 1, 0.99))
    fat = "y" * 4000
    verdict = _adapter(recorder).decide(
        _state(queue=fat, region=fat, gpu_type=fat, plan_tier=fat, job_id=fat, act=fat, worker_availability=fat)
    )
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "state_too_large"
    assert verdict["evidence"]["limit"] == "bytes"
    assert recorder.calls == []


def test_bad_state_type_is_skip() -> None:
    recorder = _Recorder(result=_payload(0.99, "run", 0.99, 1, 0.99))
    verdict = _adapter(recorder).decide(["не объект"])
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "bad_state"
    assert recorder.calls == []


# -- секреты ----------------------------------------------------------------


def test_secrets_never_reach_transport() -> None:
    recorder = _Recorder(result=_payload(0.95, "run", 0.9, 2, 0.9))
    state = _state(
        api_secret="sk_live_do_not_send",
        webhook_secret="whsec_do_not_send",
        nested={"authorization": "Bearer do_not_send", "dsn": "postgresql://u:p@h/db"},
        note="Bearer do_not_send",
    )
    verdict = _adapter(recorder, env=_env(TYPESAFE_API_KEY="sk_live_do_not_send")).decide(state)
    assert verdict["decision"] == "execute"

    body = json.dumps(recorder.calls[0], ensure_ascii=False)
    for leak in ("sk_live_do_not_send", "whsec_do_not_send", "postgresql://u:p@h/db", "Bearer do_not_send"):
        assert leak not in body, f"секрет ушёл в payload: {leak}"
    sent = recorder.calls[0]["state"]
    assert "api_secret" not in sent and "webhook_secret" not in sent and "nested" not in sent
    assert sent["tenant_ref"].startswith("t_") and "tenant-a" not in sent["tenant_ref"]
    assert verdict["evidence"]["redacted_paths"] >= 3


def test_key_is_not_logged(caplog: Any) -> None:
    recorder = _Recorder(result=_payload(0.95, "run", 0.9, 2, 0.9))
    with caplog.at_level(logging.DEBUG):
        verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "execute"
    for record in caplog.records:
        assert FAKE_KEY not in record.getMessage()
    assert FAKE_KEY not in json.dumps(verdict, ensure_ascii=False)


def test_state_fits_is_enforced_on_serialized_bytes() -> None:
    recorder = _Recorder(result=_payload(0.95, "run", 0.9, 2, 0.9))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == "execute"
    assert state_bytes(recorder.calls[0]["state"]) <= 24 * 1024


# -- путь официального SDK --------------------------------------------------


def _install_sdk_stub(payload: dict[str, Any]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    class _Answer:
        def __init__(self, data: dict[str, Any]) -> None:
            self.type = data.get("type")
            self.choice = data.get("choice")
            self.noul = data.get("noul")
            self.score = data.get("score")
            self.probabilities = data.get("probabilities")
            self.confidence = data.get("confidence")

    class _Response:
        def __init__(self, answers: dict[str, Any]) -> None:
            self.model = "jev-1.13.0"
            self.answers = {key: _Answer(value) for key, value in answers.items()}

    class _StubClient:
        def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
            seen["api_key"] = api_key

        def system_one(self, state: Any = None, questions: Any = None, model: Any = None) -> Any:
            seen["state"] = state
            seen["questions"] = questions
            seen["model"] = model
            return _Response(payload["answers"])

    module = types.ModuleType("typesafe_sdk")
    module.TypeSafeClient = _StubClient  # type: ignore[attr-defined]
    sys.modules["typesafe_sdk"] = module
    return seen


def test_sdk_path_is_preferred_when_installed() -> None:
    seen = _install_sdk_stub(_payload(0.93, "run", 0.9, 1, 0.9))
    try:
        client = JevClient(env=_env())
        verdict = JevAdapter(client=client).decide(_state())
    finally:
        sys.modules.pop("typesafe_sdk", None)

    assert seen["api_key"] == FAKE_KEY
    assert seen["model"] == "jev-1.13.0"
    assert sorted(seen["questions"]) == ["gate", "next", "risk"]
    assert verdict["decision"] == "execute"
    assert verdict["model"] == "jev-1.13.0"


# -- инвариант «любой сбой адаптера -> skip» и код возврата CLI -------------


def test_foreign_exception_from_transport_is_skip_not_exception() -> None:
    """Чужое исключение транспорта (не JevError) не выходит наружу: только skip."""

    def boom(url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
        raise RuntimeError(f"provider exploded, key was {FAKE_KEY}")

    verdict = JevAdapter(client=JevClient(env=_env(), transport=boom, sleep=lambda _s: None)).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "transport_error"
    assert FAKE_KEY not in json.dumps(verdict, ensure_ascii=False)


def test_failure_raised_outside_transport_is_skip_internal_error() -> None:
    """Сбой вне транспорта (своя логика клиента) тоже гасится в skip."""
    client = JevClient(env=_env(), transport=_Recorder(), sleep=lambda _s: None)

    def broken(state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"boom with {FAKE_KEY}")

    client.evaluate = broken  # type: ignore[method-assign]
    verdict = JevAdapter(client=client).decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "internal_error"
    assert FAKE_KEY not in json.dumps(verdict, ensure_ascii=False)


def test_sdk_constructor_failure_is_skip_not_exception() -> None:
    """SDK есть, но падает на конструкторе — наружу всё равно skip, ключ не течёт."""

    class _BrokenClient:
        def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
            raise RuntimeError(f"sdk init failed for {api_key}")

    module = types.ModuleType("typesafe_sdk")
    module.TypeSafeClient = _BrokenClient  # type: ignore[attr-defined]
    sys.modules["typesafe_sdk"] = module
    try:
        verdict = JevAdapter(client=JevClient(env=_env())).decide(_state())
    finally:
        sys.modules.pop("typesafe_sdk", None)

    assert verdict["decision"] == "skip"
    assert FAKE_KEY not in json.dumps(verdict, ensure_ascii=False)


def test_key_echoed_without_bearer_word_is_masked() -> None:
    """Провайдер вернул ключ в теле без слова Bearer — в detail его быть не должно."""
    from adapters.jev.client import JevBadResponse

    def echo(url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
        raise JevBadResponse(f'{{"key": "{FAKE_KEY}"}}')

    verdict = JevAdapter(client=JevClient(env=_env(), transport=echo, sleep=lambda _s: None)).decide(_state())
    assert verdict["decision"] == "skip"
    assert FAKE_KEY not in json.dumps(verdict, ensure_ascii=False)


def test_cli_exit_code_is_not_zero_on_skip(tmp_path: Path) -> None:
    """skip не должен выглядеть как успех для внешнего вызывающего: код 4, не 0."""
    import os
    import subprocess

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_state()), encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
    for name, extra in (
        ("flag_off", {"JEV_ENABLED": "0", "TYPESAFE_API_KEY": ""}),
        ("flag_on_no_key", {"JEV_ENABLED": "1", "TYPESAFE_API_KEY": ""}),
    ):
        proc = subprocess.run(
            [sys.executable, "-m", "adapters.jev.run", "--state-file", str(state_file)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={**env, **extra},
            timeout=60,
        )
        verdict = json.loads(proc.stdout)
        assert proc.returncode == 4, (name, proc.returncode, proc.stdout)
        assert verdict["decision"] == "skip"
        assert verdict["reason"] == "disabled"


def test_cli_forbidden_domain_wins_over_disabled_and_costs_no_network(tmp_path: Path) -> None:
    """Домен проверяется раньше флага включения: ни сокета, ни ключа в запросе."""
    import os
    import subprocess

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_state()), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "adapters.jev.run", "--state-file", str(state_file), "--domain", "payment"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT), "JEV_ENABLED": "0", "TYPESAFE_API_KEY": ""},
        timeout=60,
    )
    verdict = json.loads(proc.stdout)
    assert proc.returncode == 4
    assert verdict["reason"] == "forbidden_domain"


# -- нецелый risk score: явная fail-closed семантика ------------------------
#
# Контракт (contract.questions, docs/spikes/G-JEV-BRIDGE.md §2):
#   шкала дискретная 1–5, риск растёт с уровнем, `risk > 3.5` -> confirm,
#   значение вне шкалы -> skip. Правило округления каноном не задано, поэтому
#   оно зафиксировано явно в `_risk_value`: тик на .5 уходит к более
#   рискованному уровню (никакого Python round-to-even).

RISK_MATRIX: list[tuple[Any, float | None, str]] = [
    (3, 3.0, "execute"),
    (3.4, 3.0, "execute"),
    (3.4999, 3.0, "execute"),
    (3.5, 4.0, "confirm"),
    (3.5001, 4.0, "confirm"),
    (3.6, 4.0, "confirm"),
    (1, 1.0, "execute"),
    (2, 2.0, "execute"),
    (4, 4.0, "confirm"),
    (5, 5.0, "confirm"),
    (1.5, 2.0, "execute"),
    (2.5, 3.0, "execute"),
    (4.5, 5.0, "confirm"),
    ("3", 3.0, "execute"),
    ("3.5", 4.0, "confirm"),
    (" 3 ", 3.0, "execute"),
    ("  ", None, "skip"),
    ("Infinity", None, "skip"),
    ("-inf", None, "skip"),
    (5.4, None, "skip"),
    (None, None, "skip"),
    ("abc", None, "skip"),
    ("", None, "skip"),
    (True, None, "skip"),
    (False, None, "skip"),
    (float("nan"), None, "skip"),
    (float("inf"), None, "skip"),
    (float("-inf"), None, "skip"),
    ("nan", None, "skip"),
    ("inf", None, "skip"),
    (0, None, "skip"),
    (0.6, None, "skip"),
    (-1, None, "skip"),
    (5.5, None, "skip"),
    (6, None, "skip"),
    ([], None, "skip"),
    ({"a": 1}, None, "skip"),
]


@pytest.mark.parametrize("raw, expected_risk, expected_action", RISK_MATRIX)
def test_risk_value_matrix(raw: Any, expected_risk: float | None, expected_action: str) -> None:
    """`_risk_value` не бросает наружу и не выдумывает уровень вне шкалы 1–5."""
    assert _risk_value(raw) == expected_risk


@pytest.mark.parametrize("raw, expected_risk, expected_action", RISK_MATRIX)
def test_risk_score_drives_action(raw: Any, expected_risk: float | None, expected_action: str) -> None:
    """Конечный action: невалидный или неоднозначный score не даёт `execute`."""
    recorder = _Recorder(result=_risk_payload(raw))
    verdict = _adapter(recorder).decide(_state())
    assert verdict["decision"] == expected_action, (raw, verdict)
    assert len(recorder.calls) == 1
    if expected_risk is not None:
        assert verdict["evidence"]["risk_score"] == expected_risk


def test_half_ties_go_to_the_riskier_level_explicitly() -> None:
    """Тик на .5 — вверх (к риску), а не вниз по банковскому округлению."""
    assert _risk_value(2.5) == 3.0
    assert _risk_value(4.5) == 5.0
    assert _risk_value(3.5) == 4.0
    assert _risk_value(1.5) == 2.0


def test_half_up_vs_bankers_changes_level_not_action() -> None:
    """Против `round()` (half-even) действие на этой шкале не меняется — меняется сохраняемый уровень.

    `round(2.5) == 2`, half-up даёт 3 (оба — `execute`); `round(4.5) == 4`, half-up даёт 5
    (оба — `confirm`); `1.5` в обоих 2; `3.5` в обоих 4. Потребитель, читающий уровень,
    а не только action, видит смену ровно на 2.5 и 4.5.
    """
    expected = {
        1.5: (2, 2, "execute"),
        2.5: (2, 3, "execute"),
        3.5: (4, 4, "confirm"),
        4.5: (4, 5, "confirm"),
    }
    for raw, (bankers, half_up, action) in expected.items():
        assert round(raw) == bankers
        assert _risk_value(raw) == half_up
        verdict = _adapter(_Recorder(result=_risk_payload(raw))).decide(_state())
        assert verdict["decision"] == action, (raw, verdict)
        assert verdict["evidence"]["risk_score"] == half_up
        assert (round(raw) > 3.5) == (_risk_value(raw) > 3.5), raw


def test_non_finite_risk_never_escapes_as_exception() -> None:
    """NaN/±inf — вне шкалы: `skip`, а не ValueError/OverflowError наружу."""
    for raw in (float("nan"), float("inf"), float("-inf"), "nan", "inf"):
        recorder = _Recorder(result=_risk_payload(raw))
        verdict = _adapter(recorder).decide(_state())
        assert verdict["decision"] == "skip"
        assert verdict["reason"] in ("bad_answer", "risk_scale")


def test_bool_risk_is_not_a_number() -> None:
    for raw in (True, False):
        assert _risk_value(raw) is None
        verdict = _adapter(_Recorder(result=_risk_payload(raw))).decide(_state())
        assert verdict["decision"] != "execute"


def test_risk_out_of_scale_never_executes() -> None:
    for raw in (0, 0.6, -1, 5.5, 6, 1e300, None, "abc"):
        verdict = _adapter(_Recorder(result=_risk_payload(raw))).decide(_state())
        assert verdict["decision"] != "execute", raw


# -- run.py --out: ошибка записи результата ---------------------------------


class _AdapterSpy:
    """Двойник адаптера для CLI: считает вызовы и отдаёт заданный вердикт."""

    instances: list["_AdapterSpy"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.decide_calls = 0
        self.verdict: dict[str, Any] = {
            "decision": "execute",
            "reason": "gate_and_choice",
            "evidence": {"risk_score": 2.0},
            "model": "jev-1.13.0",
            "latency_ms": 1.0,
        }
        _AdapterSpy.instances.append(self)

    def decide(self, raw_state: Any, question_ids: Any = None, domain: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self.decide_calls += 1
        return dict(self.verdict)


def _run_cli(monkeypatch: Any, tmp_path: Path, out: Path, decision: str = "execute") -> int:
    import adapters.jev.run as jev_run

    _AdapterSpy.instances.clear()

    def _factory(*args: Any, **kwargs: Any) -> _AdapterSpy:
        spy = _AdapterSpy()
        spy.verdict["decision"] = decision
        return spy

    monkeypatch.setattr(jev_run, "JevAdapter", _factory)
    monkeypatch.setenv("JEV_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE_KEY)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_state()), encoding="utf-8")
    return jev_run.main(["--state-file", str(state_file), "--out", str(out)])


def test_out_success_preserves_result_and_exit_codes(monkeypatch: Any, tmp_path: Path) -> None:
    import adapters.jev.run as jev_run

    out = tmp_path / "decision.json"
    assert _run_cli(monkeypatch, tmp_path, out, decision="execute") == 0
    assert json.loads(out.read_text(encoding="utf-8"))["decision"] == "execute"
    assert len(_AdapterSpy.instances) == 1 and _AdapterSpy.instances[0].decide_calls == 1

    out2 = tmp_path / "skip.json"
    assert _run_cli(monkeypatch, tmp_path, out2, decision="skip") == jev_run.EXIT_SKIP
    assert json.loads(out2.read_text(encoding="utf-8"))["decision"] == "skip"

    # успешная запись по-прежнему перезаписывает существующий файл целиком
    out.write_text("прежнее содержимое", encoding="utf-8")
    assert _run_cli(monkeypatch, tmp_path, out, decision="execute") == 0
    assert json.loads(out.read_text(encoding="utf-8"))["decision"] == "execute"


def test_out_permission_error_is_output_error_not_skip(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    import adapters.jev.run as jev_run

    out = tmp_path / "decision.json"

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(jev_run.os, "replace", _boom)
    code = _run_cli(monkeypatch, tmp_path, out, decision="execute")
    err = capsys.readouterr().err
    assert code == jev_run.EXIT_OUTPUT_ERROR
    assert code != jev_run.EXIT_SKIP
    assert len(_AdapterSpy.instances) == 1 and _AdapterSpy.instances[0].decide_calls == 1
    assert not out.exists()
    assert [item.name for item in tmp_path.iterdir() if ".tmp" in item.name] == []
    assert "execute" not in err


def test_out_generic_oserror_no_retry_and_masked(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    import adapters.jev.run as jev_run

    out = tmp_path / "decision.json"

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError(f"disk on fire: {FAKE_KEY}")

    monkeypatch.setattr(jev_run.os, "replace", _boom)
    code = _run_cli(monkeypatch, tmp_path, out, decision="confirm")
    err = capsys.readouterr().err
    assert code == jev_run.EXIT_OUTPUT_ERROR
    assert len(_AdapterSpy.instances) == 1 and _AdapterSpy.instances[0].decide_calls == 1
    assert FAKE_KEY not in err
    assert "<redacted>" in err
    assert "disk on fire" in err  # диагностическая причина остаётся читаемой
    assert not out.exists()


def test_out_real_write_failure_leaves_no_partial_and_no_temp(tmp_path: Path) -> None:
    import os
    import subprocess

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    out = blocker / "decision.json"
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(_state()), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "adapters.jev.run", "--state-file", str(state_file), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT), "JEV_ENABLED": "0", "TYPESAFE_API_KEY": ""},
        timeout=60,
    )
    assert proc.returncode == 5
    assert proc.returncode != 4
    assert not out.exists()
    assert [item.name for item in tmp_path.iterdir() if ".tmp" in item.name] == []


def test_out_existing_file_survives_failed_write(monkeypatch: Any, tmp_path: Path) -> None:
    import adapters.jev.run as jev_run

    out = tmp_path / "decision.json"
    out.write_text('{"decision": "previous"}\n', encoding="utf-8")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("nope")

    monkeypatch.setattr(jev_run.os, "replace", _boom)
    assert _run_cli(monkeypatch, tmp_path, out) == jev_run.EXIT_OUTPUT_ERROR
    assert json.loads(out.read_text(encoding="utf-8"))["decision"] == "previous"


# -- регрессия exception-контракта -----------------------------------------


def test_keyboard_interrupt_and_system_exit_propagate() -> None:
    for exc in (KeyboardInterrupt(), SystemExit(2)):
        with pytest.raises(type(exc)):
            _adapter(_Recorder(error=exc)).decide(_state())


def _adapter_with_broken_evaluate() -> JevAdapter:
    """Ошибка вне транспорта: падает `evaluate`, а не транспортная функция."""
    client = JevClient(env=_env(), transport=_Recorder(), sleep=lambda _seconds: None)

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    client.evaluate = _boom  # type: ignore[method-assign]
    return JevAdapter(client=client)


def test_runtime_error_is_skip_internal_stage() -> None:
    verdict = _adapter_with_broken_evaluate().decide(_state())
    assert verdict["decision"] == "skip"
    assert verdict["reason"] == "internal_error"
    assert verdict["evidence"]["stage"] == "internal"


def test_transport_and_internal_are_distinguishable_by_reason_and_stage() -> None:
    transport = _adapter(_Recorder(error=JevTimeout("slow"))).decide(_state())
    internal = _adapter_with_broken_evaluate().decide(_state())
    assert transport["reason"] == "timeout" and transport["evidence"]["stage"] == "transport"
    assert internal["reason"] == "internal_error" and internal["evidence"]["stage"] == "internal"
    assert (transport["reason"], transport["evidence"]["stage"]) != (internal["reason"], internal["evidence"]["stage"])


MALFORMED_ANSWERS: list[dict[str, Any]] = [
    {},
    {"answers": {}},
    {"answers": {"gate": {"type": "noul", "noul": 0.99}}},
    {"answers": {"gate": {"type": "noul", "noul": 0.99}, "next": {"type": "choice", "choice": "run"}}},
    {"answers": {"gate": {"type": "noul", "noul": 0.99}, "next": {"type": "choice", "choice": "run", "confidence": 0.99}}},
    {"answers": {"gate": {"type": "noul", "noul": "yes"}, "next": {"type": "choice", "choice": "run", "confidence": 0.99}, "risk": {"type": "score", "score": 1}}},
    {"answers": {"gate": {"type": "noul", "noul": 0.99}, "next": {"type": "choice", "choice": "explode", "confidence": 0.99}, "risk": {"type": "score", "score": 1}}},
    {"answers": {"gate": {"type": "noul", "noul": 0.99}, "next": {"type": "choice", "choice": "run", "confidence": "high"}, "risk": {"type": "score", "score": 1}}},
    {"answers": {"gate": {"type": "noul", "noul": 0.99}, "next": {"type": "choice", "choice": "run", "confidence": 0.99}, "risk": {"type": "score"}}},
]


@pytest.mark.parametrize("payload", MALFORMED_ANSWERS)
def test_malformed_answer_never_executes(payload: dict[str, Any]) -> None:
    verdict = _adapter(_Recorder(result=payload)).decide(_state())
    assert verdict["decision"] != "execute"
    assert verdict["decision"] in ("skip", "confirm", "abort")
