# Tutorial 4: Full Job Lifecycle

**ROMA v2.1.0** | Time: 15 minutes | Difficulty: Intermediate

## Overview

Complete end-to-end flow: **submit → monitor → complete → bill**.

## Lifecycle Diagram

```
                   ┌──────────────────────────────────────────┐
                   │           BILLING CHECKPOINT             │
                   │  _check_spend_cap() → allow/deny         │
                   └──────────────────────────────────────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   ▼
              ALLOWED                              DENIED (402)
                    │                                   │
                    ▼                                   ▼
            POST /submit                    SpendCapExceeded
         _increment_usage()                   → upgrade plan
         (estimated cost)                     → wait for reset
                    │
                    ▼
            GET /status/{id}
            (poll every 2s)
                    │
                    ▼
            POST /complete/{id}
         _increment_usage()
         (actual GPU-sec +
          output tokens)
                    │
                    ▼
            GET /usage
         (final balance)
```

## Step 1: Pre-Submit Balance Check

```bash
# Check current usage
curl -s http://localhost:8900/usage \
  -H "x-api-key: roma-demo-key-2026" | jq .
```

```json
{
  "balance_usd": 0.00,
  "spend_cap_usd": 0.36,
  "plan": "pro",
  "remaining_cap_usd": 0.36
}
```

## Step 2: Submit Job with Token Estimate

```bash
JOB=$(curl -s -X POST http://localhost:8900/submit \
  -H "x-api-key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "LLM batch inference — 1000 documents",
    "gpu_required": true,
    "gpu_type": "A100",
    "input_tokens": 50000,
    "output_tokens": 15000,
    "plan": "pro",
    "priority": 5
  }')

JOB_ID=$(echo $JOB | jq -r '.job_id')
EST_COST=$(echo $JOB | jq -r '.estimated_cost_usd')
echo "Job: $JOB_ID, estimated cost: \$$EST_COST"
```

Response:
```
Job: uuid-abc123, estimated cost: $0.080
```

## Step 3: Poll Status

```bash
# Monitor until complete (max 30s)
for i in {1..15}; do
  STATUS=$(curl -s http://localhost:8900/status/$JOB_ID \
    -H "x-api-key: roma-demo-key-2026" | jq -r '.status')
  echo "[$i] Status: $STATUS"
  if [ "$STATUS" = "completed" ]; then break; fi
  sleep 2
done
```

## Step 4: Complete — Actual Billing

```bash
# Triggers recalculation with actual elapsed time
curl -s -X POST http://localhost:8900/complete/$JOB_ID \
  -H "x-api-key: roma-demo-key-2026" | jq .
```

What happens:
1. Calculates actual GPU seconds: `completed_at - created_at`
2. Updates `output_tokens` (if different from estimate)
3. Calls `_increment_usage()` with real values
4. Records final cost in `BillingLedger`

## Step 5: Verify Final Balance

```bash
curl -s http://localhost:8900/usage \
  -H "x-api-key: roma-demo-key-2026" | jq '{balance, cost, remaining}'
```

```json
{
  "balance": 0.080,
  "cost": 0.080,
  "remaining": 0.28
}
```

## Python Full Example

```python
from examples.sdk.sdk import ROMAClient, SpendCapExceeded

client = ROMAClient(api_key="roma-demo-key-2026")

# Pre-check
usage = client.get_usage()
print(f"Starting: ${usage['balance_usd']:.4f} / ${usage['spend_cap_usd']:.2f}")

# Submit
try:
    job = client.submit_job(
        task="LLM inference batch",
        gpu_required=True,
        input_tokens=50000,
        output_tokens=15000,
        plan="pro",
    )
except SpendCapExceeded as e:
    print(f"Blocked at spend cap: {e}")
    exit(1)

print(f"Job {job['job_id']}: ${job['estimated_cost_usd']:.6f}")

# Monitor
import time
for i in range(10):
    status = client.job_status(job['job_id'])
    if status['status'] == 'completed':
        break
    time.sleep(2)

# Complete
client.complete_job(job['job_id'])

# Final
usage2 = client.get_usage()
print(f"Final: ${usage2['balance_usd']:.4f} (charged ${usage2['balance_usd'] - usage['balance_usd']:.6f})")
```

## SDK Reference

- [Python SDK](../../examples/sdk/sdk.py)
- [JavaScript SDK](../../examples/sdk/sdk.js)
- [Go SDK](../../examples/sdk/sdk.go)

## Next Steps

- [API Reference](../api-reference.md)
- [Billing Documentation](../billing.md)
- [OpenAPI Schema](../../openapi.json)
