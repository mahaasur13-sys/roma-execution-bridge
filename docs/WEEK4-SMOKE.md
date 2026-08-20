# DecisionOS — Week 4 Smoke Test Checklist

## A. Service Up
- [x] GET /health → 200, pg=true
- [x] GET /metrics → 200

## B. Auth / Gate
- [x] /submit без ключа → 401
- [x] /submit с валидным ключом → 200, job_id+decision_id
- [x] Over-quota → 402 + deny reason

## C. Persistence
- [x] Job создан → статус queued
- [x] Restart сервиса → job сохранён
- [x] GET /status/{job_id} → тот же job

## D. Decisions API
- [x] POST /v1/decisions → decision_id + job_id
- [x] GET /v1/decisions/{id} → decision + linked job
- [x] GET /v1/decisions → list with filters
- [x] POST /v1/decisions/evaluate → dry-run, no job

## E. Policy Engine
- [x] evaluate_policies() → allowed/denied + reason
- [x] Read tools → allowed (list_workers, get_usage)
- [x] Unknown tools → denied
- [x] Over-budget → denied

## F. Transition Guard
- [x] queued→running ✅, running→completed ✅
- [x] running→failed ✅, queued→cancelled ✅
- [x] completed→running ❌, failed→running ❌

## G. Audit Events
- [x] decision.allowed, decision.denied
- [x] policy.allowed, policy.denied
- [x] transition.denied
- [x] tool_call.allowed, tool_call.denied
- [x] tenant_id корректный

## H. White-label + Tiers
- [x] get_white_label_config() returns per-tenant branding
- [x] TierBackedGate reads limits from tiers.json
- [x] Start/Pro/Enterprise profiles available

## I. Backward Compatibility
- [x] /submit старый контракт работает
- [x] /status, /jobs, /usage — без изменений
- [x] /api/chat/stream не сломан

---

**Result:** 15/15 ✅  |  **Date:** 2026-08-19
