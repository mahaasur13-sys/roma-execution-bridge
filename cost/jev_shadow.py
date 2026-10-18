"""cost/jev_shadow.py — R1 shadow harness (read-only Jev observer).

Jev observes :meth:`EnterpriseDecisionGate.evaluate` from the side and records a
``normal`` / ``anomaly`` choice next to the deterministic :class:`GateDecision`.
It NEVER changes the verdict — ``GateResult.ALLOWED/DENIED`` remain the source of
truth.

Off by default. Enable with ``JEV_GATE_SHADOW=1``. Transport is the migrated
OpenRouter client (:mod:`adapters.jev.client`, pinned ``typesafe/jev-1.13``),
imported lazily so the core keeps its import-time independence when the flag is off.

Trusted / Untrusted split:
    * trusted   — ``tenant_ref`` (sha256 hash, no raw id), ``plan_tier``,
      ``job_limit``, ``job_count_total`` (from ``db_adapter``), ``gpu_required``,
      ``payload_sha256``.
    * untrusted — raw payload text (``task``/``command``/``payload``/``cmd``);
      excluded by default, else sent with an ``UNTRUSTED:`` prefix when
      ``JEV_GATE_SHADOW_SEND_UNTRUSTED=1``.

Selective-prediction zones (temporary thresholds from docs/jev-integration-roma.md):
    anomaly prob >= 0.90 -> "auto"; 0.70..0.90 -> "review"; < 0.70 -> "normal" (fail-closed).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.jev_observer import submit_observation
from core.jev_shadow_log import record
from monitoring.metrics import track_jev_shadow

logger = logging.getLogger("roma.gate.jev_shadow")

# ── Config ───────────────────────────────────────────────────────────────
_ENV_FLAG = "JEV_GATE_SHADOW"
_SEND_UNTRUSTED_ENV = "JEV_GATE_SHADOW_SEND_UNTRUSTED"
_POINT = "R1"

# Temporary thresholds (docs/jev-integration-roma.md, R1 row). Real values only
# after shadow-mode produces a labeled dataset (see G-JEV-BRIDGE-SPIKE-REPORT.md).
ANOMALY_AUTO_THRESHOLD = 0.90
ANOMALY_REVIEW_THRESHOLD = 0.70


def is_enabled() -> bool:
    """Shadow channel on/off. Default off (fail-safe: no Jev calls)."""
    return os.getenv(_ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _send_untrusted() -> bool:
    return os.getenv(_SEND_UNTRUSTED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


QUESTION: dict[str, Any] = {
    "type": "choice",
    "instructions": "Is this job submission consistent with the tenant's historical pattern and current plan limits?",
    "criteria": {
        "normal": "Looks consistent with tenant history and plan",
        "anomaly": "Unusual volume, pattern, or payload relative to history",
    },
}


@dataclass(frozen=True)
class ShadowObservation:
    """One recorded shadow decision. ``verdict`` is the deterministic source of truth."""

    choice: str
    prob_anomaly: float
    confidence: float | None
    zone: str
    verdict: str
    observed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Trusted / untrusted state assembly ───────────────────────────────────


def tenant_ref(tenant_id: str) -> str:
    """De-identified tenant reference: ``t_`` + 12 hex chars of sha256."""
    return "t_" + hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:12]


def _raw_payload(payload: dict | None) -> str:
    """Untrusted free text from the job payload. Never used as-is in state."""
    for key in ("task", "command", "payload", "cmd"):
        value = (payload or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _payload_sha256(payload: dict | None) -> str:
    raw = _raw_payload(payload)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""


def _as_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def build_state(
    tenant_ref: str,
    plan_tier: str,
    job_limit: int,
    job_count: int,
    payload: dict | None,
) -> dict[str, Any]:
    """Assemble the trusted state for Jev (pure — no I/O, unit-testable)."""
    payload = payload or {}
    state: dict[str, Any] = {
        "tenant_ref": tenant_ref,
        "plan_tier": str(plan_tier),
        "job_limit": int(job_limit),
        "job_count_total": int(job_count),
        "gpu_required": bool(payload.get("gpu_required") or payload.get("gpu", False)),
        "priority": _as_int(payload.get("priority")),
        "payload_sha256": _payload_sha256(payload),
    }
    if _send_untrusted():
        raw = _raw_payload(payload)
        if raw:
            state["untrusted_payload"] = "UNTRUSTED:" + raw[:200]
    return state


def classify_zone(prob_anomaly: float) -> str:
    """Map anomaly probability to a selective-prediction zone."""
    if prob_anomaly >= ANOMALY_AUTO_THRESHOLD:
        return "auto"
    if prob_anomaly >= ANOMALY_REVIEW_THRESHOLD:
        return "review"
    return "normal"


def capture_state(tenant_id: str, payload: dict | None) -> dict[str, Any]:
    """Fetch trusted values from ``db_adapter`` (same reads as ``cost/gate.py``)."""
    import db_adapter as db  # local import: keeps core import-time independent

    payload = payload or {}
    tenant = db.get_tenant(tenant_id) or {}
    plan = tenant.get("plan", "free")
    plans = db._load_plans() or {}
    plan_cfg = plans.get(plan, plans.get("free", {}))
    job_count = db.count_jobs_for_tenant_total(tenant_id)
    return build_state(tenant_ref(tenant_id), plan, plan_cfg.get("max_jobs", 0), job_count, payload)


# ── Observation (fail-closed) ────────────────────────────────────────────


def _error_reason(exc: Exception) -> str:
    """Фиксированный набор причин fallback (см. ``_JEV_REASONS`` в monitoring/metrics.py)."""
    from core.jev_client import JevRateLimited, JevTimeout

    if isinstance(exc, JevTimeout):
        return "timeout"
    if isinstance(exc, JevRateLimited):
        return "rate_limited"
    return "jev_error"


def _agrees(choice: str, verdict: str) -> bool:
    """R1-отображение compliant/non-compliant (то же, что COMPLIANT в scripts/jev_shadow_agreement.py)."""
    return (choice == "normal") == (verdict == "allowed")


def _track(**fields: Any) -> None:
    """Метрики — best-effort: сбой телеметрии не меняет вердикт и не бросает."""
    try:
        track_jev_shadow(_POINT, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.debug("jev gate shadow metrics failed: %r", exc)


def _record_fallback(verdict: str, reason: str, latency_s: float) -> None:
    """Fallback = наблюдения нет; пишем запись и счётчики, вердикт остаётся детерминированным."""
    _track(outcome="fallback", reason=reason)
    record(
        _POINT,
        schema=1,
        outcome="fallback",
        reason=reason,
        verdict=verdict,
        latency_ms=round(latency_s * 1000, 2),
        observed_at=datetime.now(timezone.utc).isoformat(),
    )


def observe_once(state: dict[str, Any], verdict: str, *, client: Any = None) -> ShadowObservation | None:
    """One shadow observation. Fail-closed: any error → ``None`` (verdict stands)."""
    from core.jev_client import JevClient, JevError, normalize_answers

    c = client if client is not None else JevClient()
    started = time.monotonic()
    try:
        payload = c.evaluate(state, {"anomaly": QUESTION})
    except JevError as exc:
        logger.warning("jev gate shadow failed (verdict kept): %s", exc)
        _record_fallback(verdict, _error_reason(exc), time.monotonic() - started)
        return None
    except Exception as exc:  # noqa: BLE001 — shadow must never break the hot path
        logger.warning("jev gate shadow unexpected error (verdict kept): %r", exc)
        _record_fallback(verdict, "jev_error", time.monotonic() - started)
        return None
    latency_s = time.monotonic() - started

    answer = normalize_answers(payload).get("anomaly")
    if not answer or answer.get("type") != "choice" or answer.get("value") is None:
        logger.warning("jev gate shadow: no valid 'anomaly' answer")
        _record_fallback(verdict, "no_answer", latency_s)
        return None

    probs = answer.get("probabilities") or {}
    prob_anomaly = float(probs.get("anomaly", 0.0))
    observation = ShadowObservation(
        choice=str(answer["value"]),
        prob_anomaly=round(prob_anomaly, 4),
        confidence=float(answer["confidence"]) if answer.get("confidence") is not None else None,
        zone=classify_zone(prob_anomaly),
        verdict=verdict,
    )
    logger.info(
        "jev gate shadow: verdict=%s choice=%s prob_anomaly=%.4f zone=%s",
        verdict,
        observation.choice,
        observation.prob_anomaly,
        observation.zone,
    )

    record(
        _POINT,
        schema=1,
        outcome="ok",
        reason="",
        latency_ms=round(latency_s * 1000, 2),
        choice=observation.choice,
        prob_anomaly=observation.prob_anomaly,
        zone=observation.zone,
        confidence=observation.confidence,
        verdict=observation.verdict,
        observed_at=observation.observed_at,
    )
    _track(
        outcome="ok",
        agree="yes" if _agrees(observation.choice, observation.verdict) else "no",
        latency_s=latency_s,
    )
    return observation


# ── Integration glue ─────────────────────────────────────────────────────


def _verdict_label(verdict: Any) -> str:
    result = getattr(verdict, "result", verdict)
    return str(getattr(result, "value", result))


def shadow_evaluate(gate: Any, tenant_id: str, payload: dict | None = None, *, client: Any = None) -> Any:
    """Deterministic verdict is the source of truth; Jev observes in shadow.

    Returns ``gate.evaluate(tenant_id, payload)`` unchanged. When enabled, captures
    trusted state and runs the observation in a background thread — never blocking
    the verdict on Jev latency.
    """
    verdict = gate.evaluate(tenant_id, payload)
    if not is_enabled():
        return verdict
    try:
        state = capture_state(tenant_id, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev gate shadow: state capture failed (verdict kept): %r", exc)
        _record_fallback(_verdict_label(verdict), "state_capture", 0.0)
        return verdict
    submit_observation(_observe_safely, state, _verdict_label(verdict), client, point=_POINT)
    return verdict


def _observe_safely(state: dict[str, Any], verdict: str, client: Any) -> None:
    try:
        observe_once(state, verdict, client=client)
    except Exception:  # noqa: BLE001
        logger.debug("jev gate shadow background run failed", exc_info=True)
