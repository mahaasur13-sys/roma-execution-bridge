# ROMA — Agent Memory

## System Identity

**ROMA = Closed-Loop Execution SaaS Platform**

Every action completes the full cycle:

```
Compute → Cost → Decision → Execution → Observation → Billing → Repeat
```

## System Class

- **Not**: platform, scheduler, control plane, architecture
- **Is**: operational product with deterministic compute economics
- **Status**: feature-complete, product-market fit phase ready, no longer architecture-driven

## Maturity State

| Property | Value |
|----------|-------|
| Missing layers | None — "no missing layers problem" |
| Core changes value | <1% (scheduler, consensus, event sourcing) |
| Growth vectors | Onboarding, activation, retention, monetization |
| Distribution vectors | K8s managed, cloud marketplaces, multi-region |

## Architecture (Frozen, Layer 1)

Layer 1 frozen since 2026-04-17. All Layer 2 modules exist.

## Kubernetes Deployment (Sprint 1 — Complete, 2026-04-18)

| Layer | Status | Location |
|-------|--------|---------|
| Helm Chart | ✅ v1.0.0 | `charts/roma-execution-bridge/` |
| Kustomize Overlays | ✅ | `deploy/overlays/{home-cluster,production}/` |
| Flat YAML (all-in-one) | ✅ | `deploy/manifests/all-in-one.yaml` |
| RBAC + SA | ✅ | `deploy/k8s/RBAC/roma-rbac.yaml` |
| PVC (Longhorn/Rook) | ✅ | `deploy/k8s/storage/roma-pvc.yaml` |
| ConfigMaps + Secrets | ✅ | `deploy/k8s/configmaps/, secrets/` |
| HPA manifests | ✅ | `charts/.../templates/hpa.yaml` |
| NetworkPolicy | ✅ | `charts/.../templates/rbac.yaml` |
| PrometheusRule | ✅ | `charts/.../templates/ingress.yaml` |
| Makefile k8s targets | ✅ | `make k8s-deploy-home` etc. |

**Quick deploy:**
```bash
make k8s-deploy-home        # home cluster (Longhorn)
make k8s-deploy-kustomize-prod  # production (Rook Ceph)
make k8s-status             # verify
make k8s-portforward        # localhost:8080
```

**Storage backends:** Longhorn (home), Rook Ceph Block/FS (production), MinIO S3 (artifacts)

## Growth Targets (Layer 2 Active)

- Onboarding: time-to-first-job <30s → <10s
- Activation rate increase
- Retention loops
- Pricing tiers tuning
- Enterprise contracts
- Plugin marketplace revenue

## Files

Total: 87 Python files across:
- Execution kernel (scheduler, Raft, event sourcing, K8s)
- Plugin runtime
- Cost engine
- Billing (Stripe-integrated)
- Auth (API keys, RBAC, enterprise SSO)
- SaaS control plane (org, onboarding, bootstrap)
- Dashboard (observability, projection)

## ROMA Version

v2.1.0 — Full Billing + Monitoring (2026-08-20)

## Monitoring & Observability (v2.1.0)

### Prometheus Metrics

Exported at `/metrics`. Core billing metrics:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `roma_gpu_seconds_total` | Counter | tenant_id, plan | GPU seconds consumed |
| `roma_tokens_total` | Counter | tenant_id, plan, direction | Tokens processed (input/output) |
| `roma_billing_cost_total` | Counter | tenant_id, plan, cost_type | Total cost in USD (gpu/tokens) |
| `roma_spend_cap_balance_usd` | Gauge | tenant_id, plan | Current balance for spend-cap tenants |
| `roma_spend_cap_pct` | Gauge | tenant_id, plan | Spend-cap usage percentage |
| `roma_spend_cap_blocked_total` | Counter | tenant_id, plan | Jobs blocked by spend-cap |
| `roma_job_cost_usd` | Histogram | tenant_id, plan | Per-job cost distribution |

### Instrumentation Points

- `_increment_usage()` — increments GPU/token/cost metrics
- `_check_spend_cap()` — updates spend-cap gauges, blocks counter

### Alert Rules

Location: `deploy/monitoring/alert-rules-roma.yml`

- **ROMASpendCap90** — warning at 90% spend-cap
- **ROMASpendCapExceeded** — critical when cap exceeded
- **ROMABillingErrorRate** — critical when billing errors spike
- **ROMANoMetrics** — critical when /metrics not responding

### Grafana Dashboards

Location: `deploy/monitoring/grafana/`

- `roma-dashboard.json` — API overview (10 panels)
- `roma-billing-dashboard.json` — Billing metrics (8 panels)
- `roma-billing-cloudpayments.json` — CloudPayments billing
