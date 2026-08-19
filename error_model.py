"""DecisionOS — Consistent error model (Week 4 production hardening)."""
from fastapi import HTTPException

MACHINE_READABLE_CODES = {
    "quota_exceeded": 402,
    "cost_over_budget": 402,
    "policy_violation": 403,
    "transition_denied": 409,
    "tool_not_allowed": 403,
    "tenant_not_found": 401,
    "missing_api_key": 401,
    "invalid_api_key": 401,
    "job_not_found": 404,
    "decision_not_found": 404,
    "invalid_input": 400,
    "internal_error": 500,
}


class DecisionOSError:
    """Structured error response for all DecisionOS endpoints."""

    @staticmethod
    def raise_error(code: str, detail_override: str = "") -> None:
        status = MACHINE_READABLE_CODES.get(code, 500)
        detail = detail_override or code
        raise HTTPException(status, detail)

    @staticmethod
    def quota_exceeded(msg: str = ""):
        DecisionOSError.raise_error("quota_exceeded", msg or "Monthly job limit reached")

    @staticmethod
    def cost_over_budget(msg: str = ""):
        DecisionOSError.raise_error("cost_over_budget", msg or "Estimated cost exceeds budget")

    @staticmethod
    def tenant_not_found(msg: str = ""):
        DecisionOSError.raise_error("tenant_not_found", msg or "Tenant not found")

    @staticmethod
    def transition_denied(msg: str = ""):
        DecisionOSError.raise_error("transition_denied", msg or "Invalid status transition")
