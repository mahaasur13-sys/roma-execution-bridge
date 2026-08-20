# ROMA — Distributed Execution Platform

[![CI](https://github.com/mahaasur13-sys/roma-execution-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/mahaasur13-sys/roma-execution-bridge/actions/workflows/ci.yml)

> **ROMA = Closed-Loop Compute Economy OS v2.1.0**
> Autonomous GPU + LLM workload orchestration with full billing flow: GPU-sec counting, tiktoken-based token counting, spend-caps, and per-second/per-token charging.

---

## 🚀 Quick Start

```bash
# Start ROMA
python main.py

# Health check
curl http://localhost:8900/health

# API docs
open http://localhost:8900/docs    # Swagger UI
open http://localhost:8900/redoc   # ReDoc
```

---

## 💰 Billing (NEW in v2.1.0)

ROMA charges for **GPU seconds** and **LLM tokens** with enterprise spend-caps.

### Pricing (default rates)

| Resource | Rate | Plan Limit |
|----------|------|-------------|
| GPU-second | $0.00001/s ($0.036/h) | Free: 0s, Start: 3600s, Pro: 36000s, Enterprise: unlimited |
| Input tokens | $1 / 1M tokens | Counted via tiktoken (cl100k_base) |
| Output tokens | $2 / 1M tokens | Updated on job completion |

### Billing Flow

```
POST /submit (with input_tokens, plan)
  → _check_spend_cap()            # Refuse if over limit
  → _increment_usage()            # Estimate cost
  → MeteringEngine.record()       # Log usage event
  → BillingLedger.append()        # Debit tenant balance

POST /complete/{job_id}
  → Recalculate actual GPU-sec (elapsed time)
  → Update output_tokens
  → Final _increment_usage() call
```

### Spend-Cap Enforcement

| Plan | Monthly GPU | Spend Cap | 402 Response |
|------|------------|-----------|--------------|
| `free` | 0s | $0.00 | Immediate |
| `start` | 3600s | $0.04 | At cap |
| `pro` | 36000s | $0.36 | At cap |
| `enterprise` | Unlimited | Unlimited | Never |

Exceeding spend-cap returns `402 Payment Required`:

```json
{
  "code": "SPEND_CAP_EXCEEDED",
  "message": "Spend cap: $0.36/$0.36 (100%). Job exceeds cap.",
  "current_balance": 0.36,
  "spend_cap": 0.36
}
```

### API Examples

**Submit GPU job:**
```bash
curl -X POST http://localhost:8900/submit \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task": "train YOLOv8", "gpu_required": true, "plan": "pro", "input_tokens": 0}'
```

**Submit LLM job:**
```bash
curl -X POST http://localhost:8900/submit \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task": "Summarize document", "gpu_required": false, "plan": "pro", "input_tokens": 1500}'
```

**Check billing:**
```bash
curl http://localhost:8900/usage -H "x-api-key: $API_KEY"
```

---

## ⚡ Commands

```bash
roma run "train YOLOv8"    # Execute job
roma explain "train model"  # Cost preview  
roma logs --job-id <id>     # View logs
roma status                 # System status
```

---

## 🏗️ Architecture

```
CLI → Input Contract → ROMA Planner → Event Sourcing
    → Decision Gate (cost + quota + spend_cap) → Raft Consensus
    → Plugin Executor → GPU Scheduler → K8s/Ray
    → _increment_usage (GPU-sec + tokens) → BillingLedger
    → MeteringEngine → Dashboard
```

---

## 📦 Core Modules

| Module | Purpose |
|--------|---------|
| `auth/` | API keys + OAuth2 + RBAC + audit |
| `cost/` | Cost prediction + decision gate |
| `plugins/` | Plugin API + ML training plugin |
| `scheduler/` | GPU-aware job scheduler |
| `durability/` | Event store + event sourcing |
| `ha/` | Raft consensus + leader election |
| `k8s/` | K8s CRD + operator SDK |
| `billing/` | MeteringEngine + BillingLedger + tiktoken counting |
| `tenancy/` | Multi-tenant isolation |
| `dashboard/` | Plotly Dash UI |

---

## 🔑 Features

- **GPU-aware scheduling** — automatic CUDA node selection
- **Cost prediction** — estimated cost before execution
- **Token counting** — tiktoken-based LLM token metering
- **Spend-caps** — per-plan budget enforcement with 402 responses
- **Event-sourced execution** — full audit trail, deterministic replay
- **Raft consensus** — fault-tolerant multi-node coordination
- **Plugin ecosystem** — extensible via `IPlugin` interface
- **Multi-tenant SaaS** — org/project hierarchy with quota isolation
- **Billing engine** — GPU-sec + token metering, Stripe, CloudPayments
- **Enterprise RBAC** — role-based access with org scopes
- **CLI + Dashboard** — human CLI and visual UI

---

## 📐 System Classes

| Class | Description |
|-------|-------------|
| **Class A** | Deterministic execution kernel (strict contract) |
| **Class B** | Observable OS with full audit trail |
| **Class C** | Closed-loop compute economy (cost → billing) |

---

## 📄 Version

**v2.1.0** — Full billing flow (GPU-sec + tokens + spend-caps) — 2026-08-20
See [CHANGELOG.md](./CHANGELOG.md) for release history.
