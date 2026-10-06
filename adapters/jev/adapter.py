"""Решение адаптера: state -> один вызов system_one -> execute / confirm / abort / skip.

Адаптер ничего не возвращает в ROMA: ядро его не зовёт. Он либо запускается
отдельным процессом (см. `adapters/jev/run.py`), либо не запускается вовсе.

Жёсткие правила:

* любая ошибка транспорта, битый ответ, чужой тип ответа, низкая уверенность,
  state сверх лимита и запрещённый домен -> `skip` (не исключение и не подмена
  решения ROMA);
* `abort` и `execute` никогда не выводятся из неполных данных: нет ответа на
  `next` или нет `confidence` — это `skip`, а не догадка;
* пороги execute / confirm / abort берутся из `contract.THRESHOLDS` и живут в
  коде вызывающего, а не в модели;
* оплата, деплой, удаление и квоты адаптеру запрещены: такие вопросы не
  отправляются, а соответствующий запрос возвращает `skip` с причиной
  `forbidden_domain`.
"""

from __future__ import annotations

import math
import time
from typing import Any

from adapters.jev.client import JevClient, JevError, mask
from adapters.jev.contract import (
    DEFAULT_MODEL,
    FORBIDDEN_DOMAINS,
    LIMITS,
    NEXT_OPTIONS,
    QUESTION_IDS,
    RISK_LEVELS,
    THRESHOLDS,
    build_state,
    normalize_answers,
    oversized_field,
    questions,
    state_fits,
)

DECISIONS = ("execute", "confirm", "abort", "skip")

SKIP_REASONS = (
    "forbidden_domain",
    "bad_state",
    "state_too_large",
    "disabled",
    "timeout",
    "rate_limited",
    "bad_answer",
    "transport_error",
    "internal_error",
    "call_limit",
    "low_confidence",
)


def _verdict(decision: str, reason: str, evidence: dict[str, Any], latency_ms: float, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    return {
        "decision": decision,
        "reason": reason,
        "evidence": evidence,
        "model": model,
        "latency_ms": latency_ms,
    }


class JevAdapter:
    """Sidecar-обёртка вокруг одного вызова `system_one`."""

    def __init__(self, client: JevClient | None = None, model: str | None = None) -> None:
        self._client = client if client is not None else JevClient()
        if model:
            self._client.model = model

    def decide(
        self,
        raw_state: Any,
        question_ids: tuple[str, ...] = QUESTION_IDS,
        domain: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()

        def elapsed_ms() -> float:
            return round((time.monotonic() - started) * 1000, 3)

        effective_domain = str(domain) if domain is not None else None
        if effective_domain is None and isinstance(raw_state, dict):
            declared_act = raw_state.get("act")
            effective_domain = str(declared_act) if isinstance(declared_act, str) else None

        lowered = [str(item).lower() for item in question_ids]
        if effective_domain is not None:
            lowered.append(effective_domain.lower())
        if any(forbidden in item for item in lowered for forbidden in FORBIDDEN_DOMAINS):
            return _verdict(
                "skip",
                "forbidden_domain",
                {"questions": list(question_ids), "domain": effective_domain},
                elapsed_ms(),
            )

        try:
            state, dropped, unknown = build_state(raw_state)
        except (TypeError, ValueError):
            return _verdict("skip", "bad_state", {"questions": list(question_ids)}, elapsed_ms())

        field = oversized_field(state)
        if field is not None:
            return _verdict(
                "skip",
                "state_too_large",
                {"questions": list(question_ids), "limit": "field", "field": field},
                elapsed_ms(),
            )
        if not state_fits(state):
            return _verdict(
                "skip",
                "state_too_large",
                {"questions": list(question_ids), "limit": "bytes"},
                elapsed_ms(),
            )

        evidence: dict[str, Any] = {
            "questions": list(question_ids),
            "state_keys": sorted(key for key in state if key != "state_version"),
            "redacted_paths": len(dropped),
            "unknown_fields": unknown,
        }

        if not self._client.enabled:
            return _verdict("skip", "disabled", evidence, elapsed_ms(), self._client.model)

        try:
            payload = self._client.evaluate(state, questions(question_ids))
        except Exception as exc:  # noqa: BLE001 - инвариант: наружу только skip, не исключение
            is_transport = isinstance(exc, JevError)
            reason = _transport_reason(exc) if is_transport else "internal_error"
            return _verdict(
                "skip",
                reason,
                {
                    **evidence,
                    "stage": "transport" if is_transport else "internal",
                    "detail": mask(exc, self._client.api_key),
                },
                elapsed_ms(),
                self._client.model,
            )

        if self._client.attempts > int(LIMITS["max_calls_per_run"]) + int(LIMITS["retries"]):
            return _verdict("skip", "call_limit", {**evidence, "attempts": self._client.attempts}, elapsed_ms(), self._client.model)

        answers = normalize_answers(payload)
        evidence["answered"] = sorted(answers)

        gate = answers.get("gate")
        next_answer = answers.get("next")
        risk = answers.get("risk")

        if not gate or gate.get("type") != "noul" or not isinstance(gate.get("value"), (bool, int, float)):
            return _verdict("skip", "bad_answer", {**evidence, "reason": "gate"}, elapsed_ms(), self._client.model)
        if not next_answer or next_answer.get("type") != "choice" or next_answer.get("value") not in NEXT_OPTIONS:
            return _verdict("skip", "bad_answer", {**evidence, "reason": "next"}, elapsed_ms(), self._client.model)
        if not risk or risk.get("type") != "score" or risk.get("value") is None:
            return _verdict("skip", "bad_answer", {**evidence, "reason": "risk"}, elapsed_ms(), self._client.model)

        confidence = next_answer.get("confidence")
        if not isinstance(confidence, (int, float)):
            return _verdict("skip", "bad_answer", {**evidence, "reason": "confidence"}, elapsed_ms(), self._client.model)
        if confidence < THRESHOLDS["confidence_floor"]:
            return _verdict("skip", "low_confidence", {**evidence, "confidence": confidence}, elapsed_ms(), self._client.model)

        probability = float(gate["value"])
        choice = str(next_answer["value"])
        run_probability = _probability(next_answer.get("probabilities"), choice)
        risk_value = _risk_value(risk.get("value"))
        if risk_value is None:
            return _verdict("skip", "bad_answer", {**evidence, "reason": "risk_scale"}, elapsed_ms(), self._client.model)

        evidence.update(
            {
                "gate_noul": round(probability, 4),
                "next_choice": choice,
                "next_probability": run_probability,
                "risk_score": risk_value,
                "confidence": confidence,
            }
        )

        if risk_value > THRESHOLDS["risk_confirm"]:
            return _verdict("confirm", "risk_above_execute", evidence, elapsed_ms(), self._client.model)

        if (
            probability >= THRESHOLDS["gate_execute"]
            and choice == "run"
            and run_probability is not None
            and run_probability >= THRESHOLDS["choice_execute"]
        ):
            return _verdict("execute", "gate_and_choice", evidence, elapsed_ms(), self._client.model)

        if probability >= THRESHOLDS["gate_confirm"] or choice == "ask" or (run_probability or 0) >= THRESHOLDS["choice_confirm"]:
            return _verdict("confirm", "insufficient_signal", evidence, elapsed_ms(), self._client.model)

        return _verdict("abort", "gate_below_floor", evidence, elapsed_ms(), self._client.model)


def _transport_reason(exc: JevError) -> str:
    name = type(exc).__name__
    if name == "JevDisabled":
        return "disabled"
    if name == "JevTimeout":
        return "timeout"
    if name == "JevRateLimited":
        return "rate_limited"
    if name == "JevBadResponse":
        return "bad_answer"
    return "transport_error"


def _probability(probabilities: Any, choice: str) -> float | None:
    if not isinstance(probabilities, dict):
        return None
    value = probabilities.get(choice)
    if isinstance(value, (int, float)):
        return float(value)
    return None


RISK_MIN = min(RISK_LEVELS)
RISK_MAX = max(RISK_LEVELS)


def _risk_value(raw: Any) -> int | None:
    """Ответ `score` -> целый уровень шкалы 1-5 или None (None означает skip, не решение).

    Порядок проверок — часть контракта (fail-closed), переставлять нельзя:

    1. `bool` и `None` отсекаются до приведения: `bool` — подкласс `int`,
       поэтому `True` иначе стал бы уровнем 1 и разрешил бы execute;
    2. строка обрезается (`strip`), пустая -> None, неудачный `float()` -> None;
    3. не-finite (NaN, +-inf, включая "nan" / "inf" / "Infinity") -> None;
       `round()` на них бросает и уронил бы горячий путь;
    4. диапазонный гейт по сырому значению `[RISK_MIN, RISK_MAX]` до округления:
       иначе 0.6 стало бы 1 и ушло в execute (doc §2: «risk вне шкалы -> skip»);
    5. half-up `floor(value + 0.5)`: ровно `.5` идёт к более рискованному уровню.
       Встроенный `round()` (banker's rounding) не используется: 2.5 -> 2 и 4.5 -> 4
       занижали бы риск.

    Действие сравнивает уже целый уровень с `THRESHOLDS["risk_confirm"]` (3.5),
    то есть `level > 3.5` == `level >= 4`.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value < RISK_MIN or value > RISK_MAX:
        return None
    return math.floor(value + 0.5)


def decide(raw_state: Any, question_ids: tuple[str, ...] = QUESTION_IDS, **kwargs: Any) -> dict[str, Any]:
    """Удобная функция: один state -> одно решение."""
    return JevAdapter(**kwargs).decide(raw_state, question_ids)
