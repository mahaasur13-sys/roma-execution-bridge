# Tutorial 2: Handle 402 Payment Required

**ROMA v2.1.0** | Time: 5 minutes | Difficulty: Beginner

## What is 402?

ROMA returns `402 Payment Required` when a job would exceed the tenant's **spend cap**. This prevents surprise bills and enforces plan limits.

## Response Schema

```json
{
  "error": "spend_cap_exceeded",
  "detail": "Spend cap: $0.36/$0.36 (100%). Job exceeds cap.",
  "remaining_cap": 0.00,
  "upgrade_url": "/billing/upgrade"
}
```

## Handling in Python

```python
from examples.sdk.sdk import ROMAClient, SpendCapExceeded

client = ROMAClient(api_key="your-key")

try:
    job = client.submit_job(task="train model", gpu_required=True, plan="pro")
except SpendCapExceeded as e:
    # Option A: Notify user and redirect to upgrade
    print(f"❌ Spend cap hit: {e}")
    print("→ Redirecting to billing upgrade page...")
    # open_browser("/billing/upgrade")

    # Option B: Queue job for next billing cycle
    print("→ Queueing job for next month...")
    # db.save_deferred_job(task)

    # Option C: Downgrade to free-tier (no GPU)
    print("→ Retrying without GPU...")
    job = client.submit_job(task="train model", gpu_required=False, plan="free")
```

## Handling in JavaScript

```javascript
try {
  const job = await client.submitJob({ task: "train model", gpuRequired: true, plan: "pro" });
} catch (e) {
  if (e instanceof SpendCapExceeded) {
    console.error(`❌ Spend cap hit! Remaining: $${e.remaining}`);
    // Show upgrade modal in UI
    showUpgradeModal();
  }
}
```

## Handling in Go

```go
job, err := client.SubmitJob("train model", true, "any", 0, 0, "pro")
if err != nil {
    if capErr, ok := err.(*SpendCapError); ok {
        fmt.Printf("❌ Spend cap: %v\nUpgrade at /billing/upgrade\n", capErr)
        os.Exit(0)
    }
    log.Fatal(err)
}
```

## Alert Thresholds

ROMA warns at **90% spend cap**:

```
[WARNING] spend_cap_90% tenant=demo plan=pro balance=0.3240 cap=0.36
```

Monitor `alert_90pct` in `/usage` response.

## Recovery Paths

1. **Upgrade plan** — `/billing/create-checkout-session?plan=enterprise`
2. **Wait for reset** — billing cycles reset monthly
3. **Request override** — enterprise tenants can request temporary cap increase
4. **Queue deferred** — save job for next billing cycle

## Next Steps

→ [Tutorial 3: Check Balance and Usage](03-check-balance-and-usage.md)
