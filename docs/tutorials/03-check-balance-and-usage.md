# Tutorial 3: Check Balance and Usage

**ROMA v2.1.0** | Time: 5 minutes | Difficulty: Beginner

## Overview

Track real-time GPU seconds, token consumption, cost, and spend-cap status.

## API Endpoint

```
GET /usage
Headers: x-api-key: <your-key>
```

## Response

```json
{
  "tenant_id": "demo-billing",
  "total_gpu_seconds": 3600.0,
  "total_input_tokens": 1500000,
  "total_output_tokens": 750000,
  "total_cost_usd": 3.036,
  "spend_cap_usd": 50.00,
  "plan": "pro",
  "balance_usd": 3.036,
  "remaining_cap_usd": 46.964,
  "alert_90pct": false
}
```

## Field Reference

| Field | Description | Example |
|-------|-------------|---------|
| `total_gpu_seconds` | Cumulative GPU seconds | 3600.0 |
| `total_input_tokens` | LLM input tokens counted | 1,500,000 |
| `total_output_tokens` | LLM output tokens (post-completion) | 750,000 |
| `total_cost_usd` | Total USD charged | 3.036 |
| `spend_cap_usd` | Plan limit | 50.00 |
| `balance_usd` | Current balance (debits - credits) | 3.036 |
| `remaining_cap_usd` | Budget left under cap | 46.964 |
| `alert_90pct` | `true` when balance ≥ 90% of cap | false |

## Python: Check Before Submitting

```python
client = ROMAClient()

# Always check balance before expensive jobs
usage = client.get_usage()
if usage['balance_usd'] > usage['spend_cap_usd'] * 0.8:
    print(f"⚠️  {usage['balance_usd']:.2f}/{usage['spend_cap_usd']:.2f} used")

estimated = 0.0015  # GPU job cost estimate
if usage['remaining_cap_usd'] < estimated:
    print("❌ Not enough budget")
else:
    job = client.submit_job(task="train model", gpu_required=True)
```

## JavaScript: Dashboard Widget

```javascript
async function renderUsageWidget() {
  const usage = await client.getUsage();
  const pct = (usage.balance_usd / usage.spend_cap_usd * 100).toFixed(0);

  return `
    <div class="usage-widget" style="color: ${usage.alert_90pct ? 'red' : 'green'}">
      $${usage.balance_usd.toFixed(2)} / $${usage.spend_cap_usd.toFixed(2)}
      <div class="bar" style="width:${pct}%"></div>
      Tokens: ${usage.total_input_tokens} in / ${usage.total_output_tokens} out
      GPU: ${usage.total_gpu_seconds}s
    </div>`;
}
```

## Go: Periodic Health Check

```go
func monitorUsage(client *ROMAClient) {
    ticker := time.NewTicker(60 * time.Second)
    for range ticker.C {
        usage, err := client.GetUsage()
        if err != nil {
            log.Printf("Usage check failed: %v", err)
            continue
        }
        if usage.Alert90Pct {
            log.Printf("⚠️ 90%% spend cap: $%.2f / $%.2f", usage.BalanceUSD, usage.SpendCapUSD)
        }
    }
}
```

## Token Cost Breakdown

| Component | Rate | Example (8000/2000) |
|-----------|------|---------------------|
| GPU-sec | $0.00001/s | $0.0015 (150s) |
| Input tokens | $1 / 1M | $0.008 |
| Output tokens | $2 / 1M | $0.004 |
| **Total** | — | **$0.0135** |

## Next Steps

→ [Tutorial 4: Full Job Lifecycle](04-full-job-lifecycle.md)
