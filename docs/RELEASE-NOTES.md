# DecisionOS v1.0.0 — Release Notes

**Date:** 2026-08-19 | **Author:** Felix (@asurdev)

## What Shipped

| Week | Deliverable |
|------|------------|
| W1 | PostgreSQL primary, durable jobs, DecisionRecord, audit_events, EnterpriseDecisionGate, backward compat |
| W2 | /v1/decisions, /v1/decisions/evaluate, idempotency, retry/cancel, worker-ack, decision service |
| W3 | Policy engine, transition guard, tenant-scoped AI tool auth, expanded audit (policy_allowed/denied, transition_denied, tool_call_allowed/denied) |
| W4 | White-label, tier profiles (Start/Pro/Enterprise), TierBackedGate, OpenAPI, runbook, smoke docs |

## Not Included in v1.0.0

- MARC/MEGA agent orchestration
- SkillSpector security pre-flight
- Full main.py decomposition
- SSO/Enterprise IdP
- Plugin marketplace
- UI redesign

## Quick Smoke

```bash
curl -s http://localhost:8900/health  # {"status":"ok","pg":true}
curl -s -X POST http://localhost:8900/submit -H "X-API-Key: test-key-12345" -H "Content-Type: application/json" -d '{"command":"echo ok"}'
curl -s -X POST http://localhost:8900/v1/decisions/evaluate -H "X-API-Key: test-key-12345" -H "Content-Type: application/json" -d '{"request_type":"job_submit","payload":{"command":"test"}}'
```

## Rollback

```bash
kill $(lsof -ti :8900)
git reset --hard <prev-commit>
# unset PG_DSN to fallback to SQLite
```

---

**Status:** Production-ready  |  **Commit:** 618abf3  |  **26 files, 5398+ lines**
