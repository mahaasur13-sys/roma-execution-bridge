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
        DecisionOSError.raise_error(
            "quota_exceeded", msg or "Monthly job limit reached"
        )

    @staticmethod
    def cost_over_budget(msg: str = ""):
        DecisionOSError.raise_error(
            "cost_over_budget", msg or "Estimated cost exceeds budget"
        )

    @staticmethod
    def tenant_not_found(msg: str = ""):
        DecisionOSError.raise_error("tenant_not_found", msg or "Tenant not found")

    @staticmethod
    def transition_denied(msg: str = ""):
        DecisionOSError.raise_error(
            "transition_denied", msg or "Invalid status transition"
        )


CRYPTO_ERROR_CODES: dict[str, tuple[int, str]] = {
    "crypto_network_unsupported": (400, "Unsupported cryptocurrency network"),
    "crypto_invoice_expired": (410, "Payment invoice has expired"),
    "crypto_invoice_not_found": (404, "Payment invoice not found"),
    "crypto_payment_not_confirmed": (402, "Payment not yet confirmed on blockchain"),
    "crypto_webhook_invalid_signature": (403, "Invalid webhook signature"),
    "crypto_rate_limit_exceeded": (429, "Invoice creation rate limit exceeded"),
    "crypto_quota_exceeded": (429, "Monthly crypto invoice quota exceeded"),
    "crypto_provider_error": (502, "Crypto payment provider error"),
}

WALLET_ERROR_CODES: dict[str, tuple[int, str]] = {
    "crypto_wallet_not_found": (404, "Wallet not found"),
    "crypto_wallet_type_unsupported": (400, "Unsupported wallet type"),
    "crypto_wallet_hot_mode_blocked": (
        403,
        "HOT mode not allowed — DecisionOS never stores private keys",
    ),
    "crypto_monero_rpc_unreachable": (502, "Monero wallet RPC unreachable"),
    "crypto_monero_spend_key_blocked": (
        403,
        "Monero spend key storage blocked — view_only only",
    ),
    "crypto_wallet_compromised": (403, "Wallet is compromised — rotation required"),
    "crypto_wallet_rotation_in_progress": (409, "Wallet rotation already in progress"),
    "crypto_provider_wallet_unreachable": (502, "Provider wallet health check failed"),
}

SUPPORT_ERROR_CODES: dict[str, tuple[int, str]] = {
    "support_ticket_not_found": (404, "Support ticket not found"),
    "support_ticket_limit_reached": (429, "Maximum open tickets reached for this tier"),
    "support_invalid_transition": (400, "Invalid ticket status transition"),
    "support_closed_ticket_message": (403, "Cannot send messages to a closed ticket"),
    "support_unauthorized_role": (403, "Unauthorized role for this action"),
    "support_agent_not_found": (404, "Support agent not found"),
}
