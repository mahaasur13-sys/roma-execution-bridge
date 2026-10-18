"""plugins/builtin/jev_policy.py — R3 shadow harness (read-only Jev observer).

Jev observes :meth:`PolicyEnginePlugin.run` from the side and records a
policy-violation classification (``compliant`` / ``violation`` / ``escalate``)
next to the deterministic :class:`PolicyResult`. It NEVER changes the verdict —
``PolicyEnginePlugin`` + ``RBACEngine`` remain the source of truth.

Off by default. Enable with ``JEV_POLICY_SHADOW=1``. Transport is the migrated
OpenRouter client (:mod:`adapters.jev.client`, pinned ``typesafe/jev-1.13``),
imported lazily.

Trusted / Untrusted split:
    * trusted   — ``tenant_ref`` (sha256 hash), ``plan``, ``job_limit``,
      ``active_jobs``, ``estimated_cost`` (already passed to ``run()``).
    * untrusted — raw payload text (``task``/``command``/``payload``/``cmd``);
      excluded by default, else sent with an ``UNTRUSTED:`` prefix when
      ``JEV_POLICY_SHADOW_SEND_UNTRUSTED=1``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.jev_cascade import safe_cascade
from core.jev_observer import submit_observation

logger = logging.getLogger("roma.plugin.jev_policy")

# ── Config ───────────────────────────────────────────────────────────────
_ENV_FLAG = "JEV_POLICY_SHADOW"
_SEND_UNTRUSTED_ENV = "JEV_POLICY_SHADOW_SEND_UNTRUSTED"
_CASCADE_ENV_FLAG = "JEV_POLICY_CASCADE"
_CASCADE_CONFIDENCE = 0.85
_CASCADE_TIMEOUT_S = 2.0  # bounded blocking for the routing decision


def is_enabled() -> bool:
    """Shadow channel on/off. Default off (fail-safe: no Jev calls)."""
    return os.getenv(_ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _send_untrusted() -> bool:
    return os.getenv(_SEND_UNTRUSTED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def cascade_enabled() -> bool:
    """Cascade routing on/off. Default off; enable only after shadow data."""
    return os.getenv(_CASCADE_ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": "Does this request violate the tenant's policy (quota, cost, or custom enterprise rules)?",
    "criteria": {
        "compliant": "Within plan limits and policy",
        "violation": "Exceeds quota or violates a policy rule",
        "escalate": "Needs manual review (e.g., unusually high estimated cost)",
    },
}


@dataclass(frozen=True)
class ShadowObservation:
    """One recorded shadow decision. ``verdict`` is the deterministic source of truth."""

    choice: str
    probabilities: dict[str, float]
    confidence: float | None
    verdict: str
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Trusted / untrusted state assembly ───────────────────────────────────


def _as_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _raw_payload(payload: dict[str, Any]) -> str:
    """Untrusted free text from the payload. Never used as-is in state."""
    for key in ("task", "command", "payload", "cmd"):
        value = (payload or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _payload_sha256(payload: dict[str, Any]) -> str:
    raw = _raw_payload(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""


def build_state(tenant_ref: str, tenant: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Assemble the trusted state for Jev (pure — no I/O, unit-testable)."""
    payload = payload or {}
    tenant = tenant or {}
    state: dict[str, Any] = {
        "tenant_ref": tenant_ref,
        "plan": str(tenant.get("plan", "free")),
        "job_limit": _as_int(tenant.get("job_limit")),
        "active_jobs": _as_int(tenant.get("active_jobs")),
        "estimated_cost": _as_float(payload.get("estimated_cost")),
        "payload_sha256": _payload_sha256(payload),
    }
    if _send_untrusted():
        raw = _raw_payload(payload)
        if raw:
            state["untrusted_payload"] = "UNTRUSTED:" + raw[:200]
    return state


# ── Observation (fail-closed) ────────────────────────────────────────────


def _ask(state: dict[str, Any], *, client: Any = None, timeout: float | None = None) -> tuple[str, float | None, dict[str, float]] | None:
    """One Jev call + parse. Returns (choice, confidence, probabilities) or None (fail-closed).

    Импорт клиента, конструктор (в т.ч. пин модели), запрос и разбор ответа — внутри try:
    любая ошибка Jev → ``None``, детерминированный вердикт сохраняется.
    """
    try:
        from core.jev_client import JevClient, JevError, normalize_answers

        if client is not None:
            c = client
        elif timeout is not None:
            c = JevClient(timeout=timeout)
        else:
            c = JevClient()
        payload = c.evaluate(state, {"policy": QUESTION})
    except JevError as exc:
        logger.warning("jev policy failed (verdict kept): %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — must never break the hot path
        logger.warning("jev policy unexpected error (verdict kept): %r", exc)
        return None

    try:
        answer = normalize_answers(payload).get("policy")
        if not answer or answer.get("type") != "choice" or answer.get("value") is None:
            logger.warning("jev policy: no valid 'policy' answer")
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
    except Exception as exc:  # noqa: BLE001 — неожиданный ответ не ломает hot path
        logger.warning("jev policy parse failed (verdict kept): %r", exc)
        return None


def observe_once(state: dict[str, Any], verdict: str, *, client: Any = None) -> ShadowObservation | None:
    """One shadow observation (sync). Fail-closed: any error → ``None``."""
    parsed = _ask(state, client=client)
    if parsed is None:
        return None
    choice, confidence, probabilities = parsed
    observation = ShadowObservation(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence,
        verdict=verdict,
    )
    logger.info(
        "jev policy shadow: verdict=%s choice=%s conf=%s",
        verdict,
        observation.choice,
        observation.confidence,
    )
    from core.jev_shadow_log import record

    record(
        "R3",
        choice=observation.choice,
        probabilities=observation.probabilities,
        confidence=observation.confidence,
        verdict=observation.verdict,
        observed_at=observation.observed_at,
    )
    return observation


def cascade_once(state: dict[str, Any], *, client: Any = None, timeout: float | None = _CASCADE_TIMEOUT_S) -> str | None:
    """One synchronous Jev decision for routing.

    Returns the classification only when ``confidence >= _CASCADE_CONFIDENCE``; otherwise
    ``None`` (caller keeps the deterministic verdict). Fail-closed: any error → ``None``.
    """
    parsed = _ask(state, client=client, timeout=timeout)
    if parsed is None:
        return None
    choice, confidence, _ = parsed
    if confidence is not None and confidence >= _CASCADE_CONFIDENCE:
        logger.info("jev policy cascade: route=%s conf=%s", choice, confidence)
        return choice
    logger.info("jev policy cascade: below threshold -> deterministic (conf=%s)", confidence)
    return None


# ── Integration glue ─────────────────────────────────────────────────────


def _verdict_label(result: Any) -> str:
    return str(getattr(result, "action", result))


async def shadow_run(
    plugin: Any,
    tenant_id: str,
    payload: dict[str, Any],
    tenant: dict[str, Any],
    *,
    trace_id: str = "",
    client: Any = None,
) -> Any:
    """Deterministic ``PolicyResult`` is the source of truth; Jev observes in shadow.

    Returns ``await plugin.run(...)`` unchanged. When enabled, fires the observation
    in a background thread — never blocking the policy verdict on Jev latency.
    """
    result = await plugin.run(tenant_id, payload, tenant, trace_id=trace_id)
    if not is_enabled():
        return result
    try:
        from core.jev_client import tenant_ref

        state = build_state(tenant_ref(tenant_id), tenant, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev policy shadow: state build failed (verdict kept): %r", exc)
        return result
    submit_observation(_observe_safely, state, _verdict_label(result), client)
    return result


def _observe_safely(state: dict[str, Any], verdict: str, client: Any) -> None:
    try:
        observe_once(state, verdict, client=client)
    except Exception:  # noqa: BLE001
        logger.debug("jev policy shadow background run failed", exc_info=True)


def _escalate_for_review(result: Any, choice: str) -> Any:
    """Jev confident anomaly → escalate to manual review. Never downgrade a non-allow."""
    from plugins.builtin.policy_engine import PolicyAction, PolicyResult

    return PolicyResult(
        PolicyAction.ESCALATE,
        f"Jev cascade flagged possible policy {choice} (deterministic was allow)",
        confidence=float(getattr(result, "confidence", 1.0)),
        metadata={"jev": choice, "original_action": str(getattr(result, "action", "allow"))},
    )


async def cascade_run(
    plugin: Any,
    tenant_id: str,
    payload: dict[str, Any],
    tenant: dict[str, Any],
    *,
    trace_id: str = "",
    client: Any = None,
) -> Any:
    """Deterministic ``PolicyResult`` is the source of truth; Jev escalates in cascade.

    Escalate-only, never downgrade: a confident Jev ``violation``/``escalate`` lifts an
    ALLOW to ESCALATE (manual review); DENY/FLAG/THROTTLE/AUDIT are never overridden.
    """
    result = await plugin.run(tenant_id, payload, tenant, trace_id=trace_id)
    if not cascade_enabled():
        return result
    try:
        from core.jev_client import tenant_ref

        state = build_state(tenant_ref(tenant_id), tenant, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev policy cascade: state build failed (deterministic kept): %r", exc)
        return result
    choice = safe_cascade(None, cascade_once, state, client=client, label="policy.cascade_once")
    if choice in ("violation", "escalate") and str(getattr(result, "action", "")) == "allow":
        return safe_cascade(result, _escalate_for_review, result, choice, label="policy.escalate_for_review")
    return result


def _policy_state(tenant_ref: str, action: str, context: dict[str, Any]) -> dict[str, Any]:
    """State для live-шва policy_engine.evaluate_policies (action/context, не tenant-дикт)."""
    context = context or {}
    state: dict[str, Any] = {
        "tenant_ref": tenant_ref,
        "action": str(action),
        "estimated_cost": _as_float(context.get("estimated_cost")),
        "priority": _as_int(context.get("priority")),
        "request_type": str(context.get("request_type", "")),
        "tool_name": str(context.get("tool_name", "")),
        "job_status": str(context.get("job_status", "")),
    }
    if _send_untrusted():
        raw = _raw_payload(context)
        if raw:
            state["untrusted_payload"] = "UNTRUSTED:" + raw[:200]
    return state


def shadow_policy(
    tenant_id: str,
    action: str,
    context: dict[str, Any] | None = None,
    *,
    client: Any = None,
) -> dict[str, Any]:
    """Shadow-обёртка вокруг live policy-шва policy_engine.evaluate_policies.

    Вердикт не меняется: возвращает результат ``evaluate_policies`` как есть;
    при выключенном флаге — no-op; в shadow-режиме — только фоновое наблюдение.
    """
    from policy_engine import evaluate_policies

    result = evaluate_policies(tenant_id, action, context)
    if not is_enabled():
        return result
    if not isinstance(result, dict):
        logger.warning(
            "jev policy shadow: evaluate_policies returned non-dict (%s) — verdict kept",
            type(result).__name__,
        )
        return result
    try:
        from core.jev_client import tenant_ref

        state = _policy_state(tenant_ref(tenant_id), action, context or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev policy shadow: state build failed (verdict kept): %r", exc)
        return result
    submit_observation(_observe_safely, state, str(result.get("result", "?")), client)
    return result
