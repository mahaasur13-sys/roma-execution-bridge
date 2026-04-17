#!/usr/bin/env python3
"""ROMA Cost Decision Gate — Enforces cost boundaries before execution."""
import sys
sys.path.insert(0, '/home/workspace/roma-execution-bridge')

from cost.predictor import CostPredictor
from tenancy.manager import TenantManager

class DecisionGate:
    """
    Cost Decision Gate — blocks/approves/requires_confirmation
    before execution flows to ROMA scheduler.
    """

    def __init__(self):
        self.predictor = CostPredictor()
        self.tenancy = TenantManager()
        self.decision_history = []

    def evaluate(self, task: str, gpu_required: bool, plugin_type: str, tenant_id: str, **kwargs) -> dict:
        # Get tenant tier
        tenant_info = self.tenancy.get_tenant_info(tenant_id)
        tier = tenant_info.get("plan", "FREE") if tenant_info else "FREE"

        # Predict cost
        prediction = self.predictor.predict(task, gpu_required, plugin_type, tier, **kwargs)

        # Build gate response
        decision = prediction["decision"]
        cost = prediction["estimated_cost"]
        risk_flags = prediction["risk_flags"]

        # Build gates with full context
        gate_response = {
            "task": task,
            "tenant_id": tenant_id,
            "tier": tier,
            "estimated_cost": cost,
            "currency": "USD",
            "confidence": prediction["confidence"],
            "risk_flags": risk_flags,
            "decision": decision,
            "gpu_seconds": prediction["breakdown"]["gpu_seconds"],
            "can_proceed": decision == "APPROVED",
            "execution_blocked": decision == "REJECTED",
            "requires_confirmation": decision == "REQUIRES_CONFIRMATION",
            "approval_required": decision != "APPROVED",
            "gate_message": self._gate_message(decision, cost, tier, risk_flags),
            "suggested_action": self._suggested_action(decision, tier, cost)
        }

        self.decision_history.append(gate_response)
        return gate_response

    def _gate_message(self, decision: str, cost: float, tier: str, risk_flags: list) -> str:
        tier_limit = {"FREE": 1.0, "PRO": 50.0, "ENTERPRISE": 500.0}
        limit = tier_limit.get(tier, 10.0)

        if decision == "REJECTED":
            return f"EXECUTOR BLOCKED: cost ${cost:.4f} exceeds {tier} plan limit ${limit:.2f}"
        elif decision == "REQUIRES_CONFIRMATION":
            flags_str = f" [{', '.join(risk_flags)}]" if risk_flags else ""
            return f"CONFIRMATION REQUIRED: cost ${cost:.4f} ({tier} limit ${limit:.2f}){flags_str}"
        else:
            return f"APPROVED: cost ${cost:.4f} within {tier} plan limits"

    def _suggested_action(self, decision: str, tier: str, cost: float) -> str:
        tier_upgrade = {"FREE": "PRO", "PRO": "ENTERPRISE"}
        next_tier = tier_upgrade.get(tier, None)

        if decision == "REJECTED":
            return f"Upgrade to {next_tier} plan or reduce task duration"
        elif decision == "REQUIRES_CONFIRMATION":
            return f"Confirm with: roma run --confirm-cost\nOr upgrade to {next_tier} for higher limits"
        else:
            return "Ready to execute — proceed with roma run"

    def get_gate_status(self, tenant_id: str) -> dict:
        """Return aggregate gate statistics for tenant."""
        tenant_decisions = [d for d in self.decision_history if d["tenant_id"] == tenant_id]
        if not tenant_decisions:
            return {"total_evaluations": 0}
        return {
            "total_evaluations": len(tenant_decisions),
            "approved": sum(1 for d in tenant_decisions if d["decision"] == "APPROVED"),
            "rejected": sum(1 for d in tenant_decisions if d["decision"] == "REJECTED"),
            "requires_confirmation": sum(1 for d in tenant_decisions if d["decision"] == "REQUIRES_CONFIRMATION"),
            "total_cost_approved": sum(d["estimated_cost"] for d in tenant_decisions if d["can_proceed"])
        }


if __name__ == "__main__":
    gate = DecisionGate()

    print("=== Cost Decision Gate — Verification ===\n")

    # FREE tier — REQUIRES_CONFIRMATION (cost near limit)
    r1 = gate.evaluate(
        task="train YOLOv8 on RTX3060 for 3 epochs",
        gpu_required=True,
        plugin_type="ml_training",
        tenant_id="tenant-free"
    )
    print(f"[FREE] YOLOv8 training:")
    print(f"  Decision: {r1['decision']}")
    print(f"  Cost: ${r1['estimated_cost']:.4f}")
    print(f"  GPU-seconds: {r1['gpu_seconds']}")
    print(f"  Message: {r1['gate_message']}")
    print(f"  Action: {r1['suggested_action']}")
    print(f"  Can proceed: {r1['can_proceed']}\n")

    # PRO tier — APPROVED
    r2 = gate.evaluate(
        task="run batch inference on 10k images",
        gpu_required=True,
        plugin_type="inference",
        tenant_id="tenant-pro"
    )
    print(f"[PRO] Batch inference:")
    print(f"  Decision: {r2['decision']}")
    print(f"  Cost: ${r2['estimated_cost']:.4f}")
    print(f"  Can proceed: {r2['can_proceed']}\n")

    # FREE tier — REJECTED (cost exceeds limit)
    r3 = gate.evaluate(
        task="train LLM from scratch (full dataset)",
        gpu_required=True,
        plugin_type="ml_training",
        tenant_id="tenant-free",
        custom_duration=36000
    )
    print(f"[FREE] LLM training:")
    print(f"  Decision: {r3['decision']}")
    print(f"  Cost: ${r3['estimated_cost']:.4f}")
    print(f"  Execution blocked: {r3['execution_blocked']}")
    print(f"  Message: {r3['gate_message']}")
    print(f"  Action: {r3['suggested_action']}\n")

    # Status check
    print(f"Gate stats for tenant-free: {gate.get_gate_status('tenant-free')}")