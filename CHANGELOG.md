# ROMA Changelog

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
