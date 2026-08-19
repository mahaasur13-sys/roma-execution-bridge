"""DecisionOS — Policy Engine (root-level, sandbox-safe)."""
import db_adapter as db
from tier_profiles import load_tiers, DEFAULT_TIERS

# Allowed read tools per tier mode
READ_TOOLS = {"list_workers", "list_jobs", "get_job_status", "get_usage", "get_daily_stats"}
WRITE_TOOLS = {"submit_task", "cancel_job", "drain_worker", "slurm_status", "slurm_cancel", "create_checkout_session"}
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
        return {"result": "denied", "reason": "tenant_not_found", "policy_name": "default"}

    tier_name = tenant.get("tier", "start")
    tiers = load_tiers()
    tier = tiers.get(tier_name, DEFAULT_TIERS["start"])

    # ── tool.call ──
    if action == "tool.call":
        tool_name = ctx.get("tool_name", "")
        mode = tier.get("ai_tools_mode", "read_only")
        allowed_tools = TIER_TOOL_ACCESS.get(mode, READ_TOOLS)
        if tool_name not in allowed_tools:
            return {"result": "denied", "reason": f"tool_not_allowed: {tool_name}", "policy_name": "tool_policy"}
        # Budget check
        budget = tier.get("budget_limit", 100.0)
        if budget > 0:
            est_cost = ctx.get("estimated_cost", 0.01)
            if est_cost > budget:
                return {"result": "denied", "reason": "cost_over_budget", "policy_name": "tool_policy"}
        return {"result": "allowed", "reason": "read tool allowed", "policy_name": "tool_policy"}

    # ── job.submit / decision.create / decision.evaluate ──
    if action in ("job.submit", "decision.create", "decision.evaluate"):
        max_jobs = tier.get("max_jobs_month", 50)
        if max_jobs > 0:
            job_count = db.count_jobs_for_tenant(tenant_id)
            if job_count >= max_jobs:
                return {"result": "denied", "reason": "quota_exceeded", "policy_name": "quota_policy"}
        budget = tier.get("budget_limit", 100.0)
        est_cost = ctx.get("estimated_cost", 0.01)
        if budget > 0 and est_cost > budget:
            return {"result": "denied", "reason": "cost_over_budget", "policy_name": "budget_policy"}
        return {"result": "allowed", "reason": "within limits", "policy_name": "quota_policy"}

    # ── job.retry ──
    if action == "job.retry":
        current_status = ctx.get("job_status", "")
        if current_status != "failed":
            return {"result": "denied", "reason": "transition_denied: retry only from failed", "policy_name": "transition_policy"}
        return {"result": "allowed", "reason": "retry allowed", "policy_name": "transition_policy"}

    # ── job.cancel ──
    if action == "job.cancel":
        current_status = ctx.get("job_status", "")
        if current_status in ("completed", "failed", "cancelled"):
            return {"result": "denied", "reason": f"transition_denied: cannot cancel {current_status}", "policy_name": "transition_policy"}
        return {"result": "allowed", "reason": "cancel allowed", "policy_name": "transition_policy"}

    return {"result": "allowed", "reason": "default policy passed", "policy_name": "default"}
