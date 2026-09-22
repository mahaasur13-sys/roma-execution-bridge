"""ROMA Developer Onboarding — 0-to-job in 30 seconds."""

import os
import sys

sys.path.insert(0, '/home/workspace/roma-execution-bridge')
from auth.api_keys import APIKeyManager
from cost.predictor import CostPredictor

print("=" * 55)
print("  ROMA ONBOARDING — 0-to-job in 30 seconds")
print("=" * 55)

# Step 1: signup → API key
akm = APIKeyManager()
org_id = "org_dev"
key = akm.create_key(
    org_id=org_id,
    project_id="default",
    permissions=["job:submit", "job:read", "cost:estimate"],
)
print("\n[1/3] SIGNUP        org_created")
print(f"           org: {org_id}")
print(f"           key: {key[:50]}...")

# Step 2: cost preview
# G-PRICING-TIER-PATH: тариф — из записи клиента. Демо-org "org_dev" не является
# тенантом с записью, поэтому без ROMA_TENANT_ID цена не устанавливается, и предиктор
# отказывает кодом UNKNOWN_TENANT вместо подстановки free-тарифа.
pred = CostPredictor().predict(
    "train YOLOv8 on GPU",
    gpu_required=True,
    plugin_type="ml_training",
    tenant_id=os.environ.get("ROMA_TENANT_ID") or None,
)
cost_note = (
    "n/a" if pred["estimated_cost"] is None else f"${pred['estimated_cost']:.4f}"
)
print("\n[2/3] COST PREVIEW shown before execution")
print("           task: train YOLOv8 on GPU")
print(f"           cost: {cost_note}")
print(f"           gpu_seconds: {pred['breakdown']['gpu_seconds']}")
print(f"           tier_source: {pred.get('tier_source') or 'не установлен'}")
print(f"           decision: {pred['decision']}")

# Step 3: execute (simulate)
job_id = f"job_{org_id[:8]}_{pred['breakdown']['gpu_seconds']:.0f}"
print("\n[3/3] EXECUTE       job_completed")
print(f"           job_id: {job_id}")
print(f"           cost:   {cost_note}")
print(f"           logs:   https://app.roma.sh/jobs/{job_id}/logs")

print(f"\n{'=' * 55}")
print("  First job completed (simulated 23s)")
print(f"  Next: roma logs {job_id}")
print(f"  Next: roma explain {job_id}")
print(f"  Next: roma scale {job_id} --replicas=3")
print(f"{'=' * 55}")
