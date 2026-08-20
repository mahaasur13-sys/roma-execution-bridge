# ROMA Changelog

## [2.1.0] — 2026-08-20 — Full Billing Flow

**Production-ready billing with GPU-sec + token counting + spend-caps.**

### Added
- **Unified billing flow** — `_increment_usage()` in `main.py` as single billing entry point
- **Spend-cap enforcement** — `_check_spend_cap()` blocks jobs before exceeding plan budget
- **Token counting** — `billing/tokenizer.py` via tiktoken (cl100k_base), $1/M input, $2/M output
- **Completion billing** — `complete_job()` recalculates actual GPU-sec from elapsed time
- **Per-plan spend-caps** — `free`=$0, `start`=$5, `pro`=$50, `enterprise`=unlimited
- **90% spend alerts** — warning logs + `alert_90pct` flag in BillingUsage
- **402 Payment Required** — API returns `BillingError` schema when cap exceeded
- **MeteringEngine + BillingLedger** — module-level singletons, wired into `submit_task()`/`submit_job()`
- **Pre-submit balance check** — validates `billing_ledger.get_balance()` >= estimated cost
- **OpenAPI v2.1.0** — billing schemas, tenant endpoints, 402 responses
- **SDK examples** — Python/JS/Go in `examples/sdk/` with token + 402 handling
- **4 billing tests** — `tests/test_billing_increment.py` (GPU, tokens, both, zero-cost)

### Changed
- **PLANS** — keys `spend_cap_usd` + `overage_rate` added
- **RomaTaskInput** — new fields `input_tokens`, `output_tokens`, `plan`
- **RomaTaskResponse** — new fields `estimated_cost_usd`, `spend_cap_remaining`
- **README.md** — full Billing & Metering section with plan table
- **openapi.yaml** — rewritten YAML-based spec (previously JSON-only)

### Fixed
- Dead code `_increment_usage(tenant_id, gpu_sec)` at lines 679/937 — replaced with real billing
- FastAPI version: `1.0.0` → `2.1.0` in `app = FastAPI(…)`
- `db.py:_conn()` — CREATE INDEX moved to after table creation (SQLite fresh start)
- `db_adapter.py` — `psycopg2` lazy import with graceful fallback
- `auth/engine.py` — f-string backslash fix for Python 3.10/3.12 compat
- Helm chart — `servicemonitor` nil pointer, Helm install URL, binary path
- `.coderabbit.yaml` — unrecognized properties removed (high_level_summary, etc.)


## v1.2.0 (2026-08-13) — CloudPayments + Full Security Audit Fixes

**Audit-driven security hardening.** All P0/P1/P2 from AUDIT-2026-08-13 closed.

### Безопасность (P0)
- **CloudPayments replaces Stripe** — `billing/cloudpayments_client.py`, checkout + webhook endpoints
- **API key leak fixed** — masked in OAuth log (`api_key[:8]***`)
- **Admin auth fixed** — `_admin_only()` gate now checks `tenant-demo`
- **Multi-tenant data isolation** — analytics filtered by `tenant_id`

### Надёжность (P1)
- **Rate limiting** — slowapi on 6 critical endpoints (submit/billing/admin/auth/beta)
- **EmailStr validation** — Pydantic v2 in `/beta/apply`
- **SQLite connection leak fixed** — try/finally in `get_analytics_events()`
- **SQL injection fixed** — parameterized query in `get_analytics_overview()`
- **PostgreSQL migration ready** — `DATABASE_URL` detection, placeholder replacement

### Observability (P2)
- **CORS middleware** — configurable origins
- **Business Prometheus metrics** — billing, auth failures, errors, CloudPayments events
- **SQLite PRAGMA optimizations** — mmap, cache, synchronous=NORMAL
- **InstanceType enum** — Literal validation

### Breaking Changes
- `STRIPE_*` env vars → `CLOUDPAYMENTS_*`
- `/webhooks/stripe` → `/webhooks/cloudpayments`
- `/health`: `stripe_enabled` → `cloudpayments_enabled`

## v1.0.0 (2026-04-17) — First Stable Release

**ROMA = Closed-Loop Compute Economy OS** — First production-ready version.

### Core Platform
- Execution Kernel (Class A, strict input contract)
- Plugin System (IPlugin, auto-discovery, 3 built-in plugins)
- GPU Scheduler (VRAM tracking, backpressure)
- Event Sourcing (append-only log, deterministic replay)
- Raft Consensus (leader election, log replication)
- K8s Integration (CRD, operator SDK, RayJob)
- Billing Engine (metering, invoicing, ledger)

### Enterprise
- API Keys (scoped, HMAC, rotation)
- OAuth2 / SSO (SAML assertion)
- RBAC (org/project scopes)
- Audit Log (CSV + JSON, SIEM-ready)
- Multi-Tenant (org→project→tenant hierarchy)
- SOC2-ready controls

### Product
- CLI (run, explain, logs, status)
- Dashboard (Plotly Dash)
- Cost Explainability (alternatives, optimization hints)
- Developer Onboarding (0-to-job in 30 seconds)

### Architecture Freeze
Core kernel (Layers 1-4) frozen as of v1.0.0.
