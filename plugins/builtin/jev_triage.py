"""plugins/builtin/jev_triage.py — R5 triage shadow (read-only Jev observer).

Jev observes ``SupportChatPlugin._detect_intent`` from the side and records a
category choice next to the deterministic heuristic intent. It NEVER changes the
routing — the existing substring heuristic remains the source of truth.

Off by default. Enable with ``JEV_TRIAGE_SHADOW=1``. Transport is the migrated
OpenRouter client (:mod:`adapters.jev.client`, pinned ``typesafe/jev-1.13``),
imported lazily.

Shadow-first (ADR rev 3 §3.1): Jev decides + logs, the heuristic still routes.
The cascade (Jev routes when confident) is a later step, after shadow data.

Trusted / Untrusted split:
    * trusted   — ``message_length`` (scalar).
    * untrusted — raw message text; excluded by default, else sent with an
      ``UNTRUSTED:`` prefix (truncated) when ``JEV_TRIAGE_SHADOW_SEND_UNTRUSTED=1``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.jev_observer import submit_observation

logger = logging.getLogger("roma.plugin.jev_triage")

# ── Config ───────────────────────────────────────────────────────────────
_ENV_FLAG = "JEV_TRIAGE_SHADOW"
_SEND_UNTRUSTED_ENV = "JEV_TRIAGE_SHADOW_SEND_UNTRUSTED"
_CASCADE_ENV_FLAG = "JEV_TRIAGE_CASCADE"
_CASCADE_CONFIDENCE = 0.90
_CASCADE_TIMEOUT_S = 2.0  # bounded blocking for the routing decision


def is_enabled() -> bool:
    """Shadow channel on/off. Default off (fail-safe: no Jev calls)."""
    return os.getenv(_ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _send_untrusted() -> bool:
    return os.getenv(_SEND_UNTRUSTED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def cascade_enabled() -> bool:
    """Cascade routing on/off. Default off; enable only after shadow data."""
    return os.getenv(_CASCADE_ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


# Real _detect_intent categories (plugins/builtin/support_chat.py).
INTENT_CATEGORIES = ("job_submit", "job_status", "billing", "plugin_help", "error_help", "general")

QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": "Classify the support message into one of the allowed intent categories.",
    "criteria": {
        "job_submit": "User wants to submit / run / train a job",
        "job_status": "User asks about job status or a job ID",
        "billing": "Payment, invoice, crypto, subscription",
        "plugin_help": "Plugin install or marketplace",
        "error_help": "An error, failure, or bug",
        "general": "Anything else",
    },
}


@dataclass(frozen=True)
class ShadowObservation:
    """One recorded shadow decision. ``heuristic_intent`` is the routing source of truth."""

    choice: str
    probabilities: dict[str, float]
    confidence: float | None
    heuristic_intent: str
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Trusted / untrusted state assembly ───────────────────────────────────


def build_state(message: str) -> dict[str, Any]:
    """Assemble the trusted state for Jev (pure — no I/O, unit-testable)."""
    state: dict[str, Any] = {"message_length": len(message or "")}
    if _send_untrusted():
        raw = (message or "").strip()
        if raw:
            state["untrusted_message"] = "UNTRUSTED:" + raw[:400]
    return state


# ── Observation (fail-closed) ────────────────────────────────────────────


def _ask(message: str, *, client: Any = None, timeout: float | None = None) -> tuple[str, float | None, dict[str, float]] | None:
    """One Jev call + parse. Returns (choice, confidence, probabilities) or None (fail-closed).

    Import, конструктор клиента (включая пин модели) и разбор ответа — внутри try:
    любая ошибка → ``None``.
    """
    try:
        from core.jev_client import JevClient, JevError, normalize_answers

        if client is not None:
            c = client
        elif timeout is not None:
            c = JevClient(timeout=timeout)
        else:
            c = JevClient()
        payload = c.evaluate(build_state(message), {"triage": QUESTION})
    except JevError as exc:
        logger.warning("jev triage failed (heuristic kept): %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — must never break the hot path
        logger.warning("jev triage unexpected error (heuristic kept): %r", exc)
        return None

    try:
        answer = normalize_answers(payload).get("triage")
        if not answer or answer.get("type") != "choice" or answer.get("value") is None:
            logger.warning("jev triage: no valid 'triage' answer")
            return None

        probs = answer.get("probabilities") or {}
        probabilities = {str(k): float(v) for k, v in probs.items()}
        choice = str(answer["value"])
        confidence = answer.get("confidence")
        if confidence is None:
            confidence = probabilities.get(choice, 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None
        return choice, confidence, probabilities
    except Exception as exc:  # noqa: BLE001 — неожиданный ответ → None
        logger.warning("jev triage parse failed (heuristic kept): %r", exc)
        return None


def observe_once(message: str, heuristic_intent: str, *, client: Any = None) -> ShadowObservation | None:
    """One shadow observation (sync). Fail-closed: any error → ``None``."""
    parsed = _ask(message, client=client)
    if parsed is None:
        return None
    choice, confidence, probabilities = parsed
    observation = ShadowObservation(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence,
        heuristic_intent=heuristic_intent,
    )
    logger.info(
        "jev triage shadow: heuristic=%s choice=%s conf=%s",
        heuristic_intent,
        observation.choice,
        observation.confidence,
    )
    from core.jev_shadow_log import record

    record(
        "R5",
        choice=observation.choice,
        probabilities=observation.probabilities,
        confidence=observation.confidence,
        verdict=observation.heuristic_intent,
        observed_at=observation.observed_at,
    )
    return observation


def cascade_once(message: str, *, client: Any = None, timeout: float | None = _CASCADE_TIMEOUT_S) -> str | None:
    """One synchronous Jev decision for routing.

    Returns the category only when ``confidence >= _CASCADE_CONFIDENCE``; otherwise
    ``None`` (caller falls back to the heuristic). Fail-closed: any error → ``None``
    (сеть, таймаут, ``JevError`` с ``reason="model_pin"`` / ``"model_pin_response"``,
    неожиданный ответ, исключение в конструкторе клиента) — вызывающий сохраняет
    исходный intent.
    """
    parsed = _ask(message, client=client, timeout=timeout)
    if parsed is None:
        return None
    choice, confidence, _ = parsed
    if confidence is not None and confidence >= _CASCADE_CONFIDENCE:
        logger.info("jev triage cascade: route=%s conf=%s", choice, confidence)
        return choice
    logger.info("jev triage cascade: below threshold -> heuristic (conf=%s)", confidence)
    return None


# ── Integration glue ─────────────────────────────────────────────────────


def shadow_triage(message: str, heuristic_intent: str, *, client: Any = None) -> None:
    """Fire-and-forget shadow observation (never blocks routing)."""
    if not is_enabled():
        return
    submit_observation(_observe_safely, message, heuristic_intent, client)


def _observe_safely(message: str, heuristic_intent: str, client: Any) -> None:
    try:
        observe_once(message, heuristic_intent, client=client)
    except Exception:  # noqa: BLE001
        logger.debug("jev triage shadow background run failed", exc_info=True)
