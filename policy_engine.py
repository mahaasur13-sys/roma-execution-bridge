"""DecisionOS — Policy Engine (root-level, sandbox-safe)."""

import db_adapter as db
from tier_profiles import load_tiers, DEFAULT_TIERS

# Allowed read tools per tier mode
READ_TOOLS = {
    "list_workers",
    "list_jobs",
    "get_job_status",
    "get_usage",
    "get_daily_stats",
}
WRITE_TOOLS = {
    "submit_task",
    "cancel_job",
    "drain_worker",
    "slurm_status",
    "slurm_cancel",
    "create_checkout_session",
}
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS

TIER_TOOL_ACCESS = {
    "none": set(),
    "read_only": READ_TOOLS,
    "scoped": READ_TOOLS | {"submit_task", "get_job_status"},
    "full": ALL_TOOLS,
}


def evaluate_policies(tenant_id: str, action: str, context: dict | None = None) -> dict:
    """Evaluate policies for tenant + action. Returns {result, reason, policy_name}."""
    ctx = context or {}
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return {
            "result": "denied",
            "reason": "tenant_not_found",
            "policy_name": "default",
        }

    tier_name = tenant.get("tier", "start")
    tiers = load_tiers()
    tier = tiers.get(tier_name, DEFAULT_TIERS["start"])

    # ── tool.call ──
    if action == "tool.call":
        tool_name = ctx.get("tool_name", "")
        mode = tier.get("ai_tools_mode", "read_only")
        allowed_tools = TIER_TOOL_ACCESS.get(mode, READ_TOOLS)
        if tool_name not in allowed_tools:
            return {
                "result": "denied",
                "reason": f"tool_not_allowed: {tool_name}",
                "policy_name": "tool_policy",
            }
        # Budget check
        budget = tier.get("budget_limit", 100.0)
        if budget > 0:
            est_cost = ctx.get("estimated_cost", 0.01)
            if est_cost > budget:
                return {
                    "result": "denied",
                    "reason": "cost_over_budget",
                    "policy_name": "tool_policy",
                }
        return {
            "result": "allowed",
            "reason": "read tool allowed",
            "policy_name": "tool_policy",
        }

    # ── job.submit / decision.create / decision.evaluate ──
    if action in ("job.submit", "decision.create", "decision.evaluate"):
        max_jobs = tier.get("max_jobs_month", 50)
        if max_jobs > 0:
            job_count = db.count_jobs_for_tenant_total(tenant_id)
            if job_count >= max_jobs:
                return {
                    "result": "denied",
                    "reason": "quota_exceeded",
                    "policy_name": "quota_policy",
                }
        budget = tier.get("budget_limit", 100.0)
        est_cost = ctx.get("estimated_cost", 0.01)
        if budget > 0 and est_cost > budget:
            return {
                "result": "denied",
                "reason": "cost_over_budget",
                "policy_name": "budget_policy",
            }
        return {
            "result": "allowed",
            "reason": "within limits",
            "policy_name": "quota_policy",
        }

    # ── job.retry ──
    if action == "job.retry":
        current_status = ctx.get("job_status", "")
        if current_status != "failed":
            return {
                "result": "denied",
                "reason": "transition_denied: retry only from failed",
                "policy_name": "transition_policy",
            }
        return {
            "result": "allowed",
            "reason": "retry allowed",
            "policy_name": "transition_policy",
        }

    # ── job.cancel ──
    if action == "job.cancel":
        current_status = ctx.get("job_status", "")
        if current_status in ("completed", "failed", "cancelled"):
            return {
                "result": "denied",
                "reason": f"transition_denied: cannot cancel {current_status}",
                "policy_name": "transition_policy",
            }
        return {
            "result": "allowed",
            "reason": "cancel allowed",
            "policy_name": "transition_policy",
        }

    if action.startswith("tool_call"):
        if not context.get("tenant_key"):
            return {
                "result": "denied",
                "reason": "AI tool call without tenant key",
                "policy_name": "default",
            }
        return {
            "result": "allowed",
            "reason": "default policy passed",
            "policy_name": "default",
        }

    if action == "crypto_create_invoice":
        tenant_id = context.get("tenant_id", "")
        network = context.get("network", "")
        if network not in ("USDT_TRC20", "USDT_ERC20", "USDC", "BTC", "TON", "SOL"):
            return {
                "result": "denied",
                "reason": f"Unsupported network: {network}",
                "policy_name": "crypto_network_guard",
            }
        max_invoices = {"start": 5, "pro": 20, "enterprise": 100}.get(
            context.get("tier", "start"), 5
        )
        if context.get("invoice_count_this_month", 0) >= max_invoices:
            return {
                "result": "denied",
                "reason": f"Crypto invoice quota ({max_invoices}/mo) exceeded",
                "policy_name": "crypto_quota",
            }
        return {
            "result": "allowed",
            "reason": "Crypto invoice creation allowed",
            "policy_name": "crypto_quota",
        }

    if action == "crypto_tier_activate":
        if context.get("payment_status") != "confirmed":
            return {
                "result": "denied",
                "reason": "Payment not confirmed",
                "policy_name": "crypto_payment_guard",
            }
        if context.get("invoice_status") != "paid":
            return {
                "result": "denied",
                "reason": "Invoice not paid",
                "policy_name": "crypto_invoice_guard",
            }
        return {
            "result": "allowed",
            "reason": "Tier activation allowed",
            "policy_name": "crypto_tier_activation",
        }

    if action.startswith("crypto_webhook:"):
        provider = context.get("provider", "")
        if provider not in ("nowpayments", "btcpay"):
            return {
                "result": "denied",
                "reason": f"Unknown webhook provider: {provider}",
                "policy_name": "crypto_webhook_provider",
            }
        return {
            "result": "allowed",
            "reason": "Crypto webhook processing allowed",
            "policy_name": "crypto_webhook",
        }

    if action == "crypto:wallet:create":
        tenant_id = context.get("tenant_id", "")
        wallet_type = context.get("wallet_type", "")
        if wallet_type not in ("provider", "self_hosted", "hardware", "monero"):
            return {
                "result": "denied",
                "reason": f"Unsupported wallet type: {wallet_type}",
                "policy_name": "crypto_wallet_type_guard",
            }
        if wallet_type == "monero" and context.get("mode") == "hot":
            return {
                "result": "denied",
                "reason": "Monero HOT mode not allowed — view_only only",
                "policy_name": "crypto_monero_privacy",
            }
        if context.get("wallets_count", 0) >= 10:
            return {
                "result": "denied",
                "reason": "Max 10 wallets per tenant",
                "policy_name": "crypto_wallet_limit",
            }
        return {
            "result": "allowed",
            "reason": "Wallet creation allowed",
            "policy_name": "crypto_wallet_create",
        }

    if action == "crypto:wallet:generate_address":
        if context.get("wallet_status") == "compromised":
            return {
                "result": "denied",
                "reason": "Wallet is compromised — rotate first",
                "policy_name": "crypto_wallet_security",
            }
        return {
            "result": "allowed",
            "reason": "Address generation allowed",
            "policy_name": "crypto_wallet_address",
        }

    if action == "crypto:wallet:generate_monero_subaddress":
        if context.get("wallet_type") != "monero":
            return {
                "result": "denied",
                "reason": "Monero subaddress requires Monero wallet",
                "policy_name": "crypto_monero_type_guard",
            }
        if context.get("wallet_mode") != "view_only":
            return {
                "result": "denied",
                "reason": "Monero subaddress requires view_only mode",
                "policy_name": "crypto_monero_privacy",
            }
        return {
            "result": "allowed",
            "reason": "Monero subaddress creation allowed",
            "policy_name": "crypto_monero_subaddress",
        }

    if action == "crypto:wallet:rotate":
        if context.get("wallet_status") == "rotating":
            return {
                "result": "denied",
                "reason": "Wallet already rotating",
                "policy_name": "crypto_wallet_busy",
            }
        return {
            "result": "allowed",
            "reason": "Wallet rotation allowed",
            "policy_name": "crypto_wallet_rotate",
        }

    return {
        "result": "allowed",
        "reason": "default policy passed",
        "policy_name": "default",
    }
