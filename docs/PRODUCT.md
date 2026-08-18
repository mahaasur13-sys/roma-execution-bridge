# ROMA Execution Bridge — Product Overview

**ROMA = Results-Oriented Management and Accountability**

> Radix Omnium Malorum Avaritia — «Корень всех зол — жадность» (историческая расшифровка)

---

## What is ROMA?

ROMA Execution Bridge — это SaaS-платформа для оркестрации GPU-вычислений с предсказуемой стоимостью и мультитенантной изоляцией.

**Compute → Cost → Decision → Execution → Observation → Billing → Repeat**

Закрытый цикл: от задачи до биллинга в одной платформе.

**Сервис:** [https://roma-execution-bridge-asurdev.zocomputer.io](https://roma-execution-bridge-asurdev.zocomputer.io)

---

## Core Flow

| Step | Action |
|------|--------|
| User → API / Dashboard | Отправка задачи через REST API или Web UI |
| Scheduler | Raft consensus + event sourcing |
| GPU Worker | Получение и выполнение задачи (WebSocket / Slurm / Ray) |
| Result | Результат возвращается пользователю |
| Billing | CloudPayments — списание согласно тарифу |

---

## Key Capabilities

| Feature | Description |
|---------|-------------|
| Multi-tenancy | Data & quota isolation between teams |
| CloudPayments | Subscriptions: Pro (4900 ₽/mo) and Enterprise (29900 ₽/mo) |
| Rate limiting | Per-tenant throttling |
| OAuth | Google / GitHub sign-in |
| Grafana dashboards | RPS, latency, error rate, billing |
| Alerts | Critical + warning alerts via Telegram |
| Execution backends | Slurm, Ray, WebSocket workers |
| GPU worker registry | Plug-and-play worker registration |
| AI Chat Assistant | Streaming LLM + 11 tool-calling functions |

---

## Pricing Plans

| Tier | Price | Quota | Features |
|------|-------|-------|---------|
| **Free** | 0 ₽ | 50 jobs/month | Community support, API access |
| **Pro** | 4 900 ₽/mo | 500 jobs/month | Priority queue, GPU access, detailed stats |
| **Enterprise** | 29 900 ₽/mo | Unlimited | Custom SLA, dedicated workers, SSO |

---

## Target Audience

| Segment | Use Case |
|---------|----------|
| **ML startups** | Fast model training without infrastructure overhead |
| **Research labs** | Reproducible experiments with fixed compute budgets |
| **Quant teams** | Backtesting & signal generation on GPU |
| **DevOps / MLOps** | Managed GPU orchestration for CI/CD pipelines |

---

## Competitive Advantages

| ROMA | Competitors |
|------|-------------|
| Fixed subscription (predictable cost) | Pay-as-you-go (unpredictable) |
| Closed-loop: job → billing in one platform | Separate tools: scheduler + billing |
| Russian payment gateway (CloudPayments) | Stripe-only |
| 2 minutes from registration to first job | Complex onboarding |
| AI Chat Assistant with tool calling | CLI or API only |

---

## Connection Architecture

### 1. Client Mode (User Laptops / Servers)

```
User → HTTPS + X-API-Key / OAuth → ROMA API
  ├── Submit jobs
  ├── View results
  └── Access dashboard
```

### 2. Worker Mode (Rented GPU Nodes)

```
GPU Node → WebSocket / Agent / Ray / Slurm → ROMA Scheduler
  ├── Register worker
  ├── Receive jobs
  └── Report results
```

---

## AI Chat Assistant

**Model:** DeepSeek V4 / V4 Flash via OpenAI-compatible API

**Capabilities:**
- Streaming token-by-token response
- 11 tool-calling functions (jobs, workers, Slurm, billing, stats)
- Persistent history (localStorage)
- Personalization (user name)
- Stop button, typing indicator, clear history
- API key modal (NGC-level UI)
- Window: 480 × 720 px, mobile fullscreen

**Tools:**

| Tool | Description | Requires API Key |
|------|-------------|:---:|
| submit_task | Submit a new ML job | Yes |
| get_job_status | Check job status | Yes |
| list_jobs | List recent jobs | Yes |
| cancel_job | Cancel a job | Yes |
| list_workers | List all workers | Yes |
| drain_worker | Drain a worker | Yes |
| slurm_status | Slurm job status | Yes |
| slurm_cancel | Cancel Slurm job | Yes |
| get_usage | Resource usage & balance | Yes |
| create_checkout_session | Top-up balance | Yes |
| get_daily_stats | Daily statistics | Yes |

---

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| /health | GET | None | System health check |
| /submit | POST | X-API-Key | Submit a new job |
| /status/{job_id} | GET | X-API-Key | Get job status |
| /jobs | GET | X-API-Key | List recent jobs |
| /cancel/{job_id} | POST | X-API-Key | Cancel job |
| /workers | GET | X-API-Key | List all workers |
| /workers/{id}/drain | POST | X-API-Key | Drain a worker |
| /slurm/status/{id} | GET | X-API-Key | Slurm job status |
| /slurm/cancel/{id} | POST | X-API-Key | Cancel Slurm job |
| /usage | GET | X-API-Key | Usage & balance |
| /billing/create-checkout-session | POST | X-API-Key | Create payment session |
| /stats/daily | GET | X-API-Key | Daily statistics |
| /api/chat/stream | POST | Optional | AI chat streaming |
| /api/auth/{provider}/login | GET | None | OAuth login |
| /api/auth/{provider}/callback | GET | None | OAuth callback |
| /metrics | GET | None | Prometheus metrics |
| /docs | GET | None | Swagger API documentation |

---

## Demo Keys

| Key | Tenant | Notes |
|-----|--------|------|
| roma-demo-key-2026 | tenant-demo | Demo tenant |
| roma-test-key-alpha | tenant-alpha | Alpha testing |
| roma-test-key-bravo | tenant-bravo | Bravo testing |
| test-key-12345 | demo | Development |

---

## Quick Start

```bash
# Submit a job
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/submit   -H "X-API-Key: roma-demo-key-2026"   -H "Content-Type: application/json"   -d '{"task": "python train.py --epochs 10", "gpu_required": true, "priority": "high"}'

# Check job status
curl -H "X-API-Key: roma-demo-key-2026"   https://roma-execution-bridge-asurdev.zocomputer.io/status/JOB_ID

# List workers
curl -H "X-API-Key: roma-demo-key-2026"   https://roma-execution-bridge-asurdev.zocomputer.io/workers
```

---

## Setting API Key in the Chat

Open the ROMA site, press F12 → Console, and paste:

```js
localStorage.setItem("roma_api_key", "roma-demo-key-2026")
```

Refresh the page. The AI assistant will now use your key for tool-calling requests.
Or use the 🔑 button in the chat header to open the **API Key Settings** modal.

---

## Best Practices

1. **Never share your API key** — rotate if compromised
2. **Use the AI assistant** — it handles tool calling, streaming, and multi-turn conversations
3. **Monitor through Grafana** — RPS, latency, and billing dashboards
4. **Set up alerts** — critical + warning thresholds via Telegram
5. **Start with demo keys** — then create your own key in Admin
6. **Use OAuth for teams** — GitHub / Google for SSO

---

## Status

- **Version:** v1.2.0
- **Production-ready:** Yes
- **Security audit:** Passed (2026-08-13)
- **SRE report:** Service UP (2026-08-17), PostgreSQL healthy
- **Current AI provider:** DeepSeek V4 / V4 Flash

---

## Notices

- ROMA is not affiliated with NVIDIA or NGC.
- API keys are stored in your browser's localStorage only.
- The AI chat assistant uses a demo backend by default — replace with your own LLM provider for production.
- Historical name "Radix Omnium Malorum Avaritia" is a Latin phrase meaning "The root of all evil is greed" — it refers to the platform's focus on predictable cost economics, not a value judgment.
- CloudPayments integration follows Russian payment regulation requirements.
