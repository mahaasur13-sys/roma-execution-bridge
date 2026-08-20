# Tutorial 1: Submit Job with Token Tracking

**ROMA v2.1.0** | Time: 10 minutes | Difficulty: Beginner

## Overview

Learn how to submit GPU and LLM jobs with token counting and spend-cap enforcement.

## Prerequisites

- ROMA running on `http://localhost:8900`
- API key (default: `roma-demo-key-2026`)

## Step 1: Submit a GPU Job (no tokens)

```bash
curl -X POST http://localhost:8900/submit \
  -H "x-api-key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "train YOLOv8 on custom dataset",
    "gpu_required": true,
    "gpu_type": "any",
    "input_tokens": 0,
    "output_tokens": 0,
    "plan": "free",
    "priority": 5
  }'
```

Response:
```json
{
  "status": "queued",
  "job_id": "uuid-xxx",
  "estimated_cost_usd": 0.0015,
  "spend_cap_remaining": 0.00
}
```

## Step 2: Submit an LLM Job (with tokens)

Token costs:
- Input: **$1 per 1M tokens**
- Output: **$2 per 1M tokens**

```bash
curl -X POST http://localhost:8900/submit \
  -H "x-api-key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Summarize quarterly report",
    "gpu_required": true,
    "gpu_type": "A100",
    "input_tokens": 8000,
    "output_tokens": 2000,
    "plan": "pro",
    "priority": 5
  }'
```

Estimated cost: `8000 × $0.000001 + 2000 × $0.000002 = $0.012`

## Step 3: Python SDK

```python
from examples.sdk.sdk import ROMAClient, SpendCapExceeded

client = ROMAClient(api_key="roma-demo-key-2026")

try:
    job = client.submit_job(
        task="LLM inference GPT-4",
        gpu_required=True,
        gpu_type="A100",
        input_tokens=8000,
        output_tokens=2000,
        plan="pro",
    )
    print(f"Job {job['job_id']}: cost ${job['estimated_cost_usd']:.6f}")
except SpendCapExceeded as e:
    print(f"Blocked: {e}")
```

## Spend Caps by Plan

| Plan | GPU-sec/month | Spend Cap | Token rates |
|------|--------------|-----------|-------------|
| `free` | 0 | $0.00 | $1M/$2M |
| `start` | 3,600 | $0.04 | $1M/$2M |
| `pro` | 36,000 | $0.36 | $1M/$2M |
| `enterprise` | Unlimited | Unlimited | Custom |

## Next Steps

→ [Tutorial 2: Handle 402 Payment Required](02-handle-payment-required.md)
