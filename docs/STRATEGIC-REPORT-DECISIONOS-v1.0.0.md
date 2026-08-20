# DecisionOS v1.0.0 — Стратегический отчёт

**Дата:** 2026-08-19  
**Автор:** Felix (@asurdev)  
**Коммит:** `c848e37` (docs: RELEASE-NOTES, WEEK4-RUNBOOK, WEEK4-SMOKE)  
**База:** ROMA Execution Bridge v1.2.0 → DecisionOS v1.0.0

---

## 1. Исполнительное резюме

DecisionOS v1.0.0 — это **enterprise-ориентированная платформа принятия решений**, выращенная из ядра ROMA Execution Bridge. Платформа добавляет три слоя, отсутствовавшие в ROMA: **политики (Policy Engine)**, **тиринг (Tier Profiles)** и **аудитабельный Decision Trail** — превращая closed-loop executor в полноценную decision infrastructure для multi-tenant SaaS.

**Ключевой результат:** 13 новых модулей, 3 уровня цен (Start / Pro / Enterprise), 3 документа (Release Notes, Runbook, Smoke Test Plan), 8 smoke-тестов, обратная совместимость со всеми существующими API ROMA.

---

## 2. Архитектура

### 2.1. Принцип гейтирования

```
Request → Auth/Tenant → Policy Engine → Decision Gate → Execution → Audit
                                                         ↓
                                              (Quota, Cost, Policy)
```

**Правило ExecutionAllowed:** `Quota OK ∧ Cost OK ∧ Policy OK`

### 2.2. Компонентная карта

| Слой | Модули | Назначение |
|------|--------|------------|
| **Policy** | `file policy_engine.py`, `file policy_transition.py` | Оценка политик per-tenant, guard разрешённых переходов статусов |
| **Tier** | `file tier_profiles.py`, `file tier_gate.py`, `file tiers.json` | Start / Pro / Enterprise с лимитами по jobs, GPU, concurrency, retention |
| **Audit** | `file audit_events.py`, `file decision_service.py`, `file decision_store.py` | 5 типов событий (policy_allowed/denied, transition_denied, tool_call_allowed/denied), персистентность в PostgreSQL |
| **Routing** | `file router_decisions.py`, `file router_jobs.py` | Декомпозиция монолитного роутера ROMA → раздельные decision/job endpoints |
| **Enterprise** | `file white_label.py`, `file error_model.py` | White-label конфигурация, унифицированная модель ошибок с machine-readable кодами |

### 2.3. Доменные сущности

- **DecisionRequest** — входящий запрос (tenant, action, context)
- **DecisionRecord** — результат (allowed/denied, reason, policy_name)
- **ExecutionJob** — задание (queued → running → completed/failed/cancelled)
- **PolicyDecision** — вердикт policy engine
- **UsageEvent** — событие потребления (для биллинга)

---

## 3. Тир-модель

| Параметр | Start | Pro | Enterprise |
|----------|-------|-----|------------|
| max_jobs_month | 50 | 150 | ∞ |
| max_gpu_seconds_month | 36 000 | 180 000 | ∞ |
| max_concurrent | 5 | 20 | 100 |
| audit_retention_days | 7 | 90 | ∞ |
| white_label | ❌ | ❌ | ✅ |
| policy_engine | default | default | custom |
| ai_tools | basic | full | full |
| sso | ❌ | ❌ | ✅ |

---

## 4. Policy Engine

### Интерфейс

```python
evaluate_policies(tenant_id: str, action: str, context: dict) -> {
    "result": "allowed" | "denied",
    "reason": str,
    "policy_name": str
}
```

### Default Policy Profile

| Действие | Правило |
|----------|---------|
| `read:*` (basic tools) | ✅ Allowed |
| `submit` | ✅ Allowed if quota available |
| `retry` | ✅ Allowed only if job status = failed |
| `cancel` | ❌ Denied if status = completed/failed/cancelled |
| `tool_call` (AI) | ❌ Denied if tenant key missing |

### Transition Guard

| Transition | Статус |
|------------|:------:|
| queued → running | ✅ |
| running → completed | ✅ |
| running → failed | ✅ |
| queued → cancelled | ✅ |
| running → cancelled | ✅ |
| completed → any | ❌ transition_denied |
| cancelled → any | ❌ transition_denied |
| failed → any (except retry flow) | ❌ transition_denied |

---

## 5. Audit Events

5 типов событий с полным контекстом (tenant_id, action, result, reason, policy_name, timestamp):

- `policy_allowed`
- `policy_denied`
- `transition_denied`
- `tool_call_allowed`
- `tool_call_denied`

Все события пишутся в PostgreSQL, retention зависит от тира.

---

## 6. Error Model

Унифицированная модель ошибок с machine-readable кодами:

| Код | HTTP | Описание |
|-----|------|----------|
| `quota_exceeded` | 429 | Лимит тира исчерпан |
| `cost_over_budget` | 402 | Превышен бюджет |
| `policy_violation` | 403 | Политика запрещает действие |
| `transition_denied` | 409 | Недопустимый переход статуса |
| `tool_not_allowed` | 403 | AI tool запрещён |
| `tenant_not_found` | 404 | Тенант не найден |

---

## 7. Технические метрики

| Метрика | Значение |
|---------|----------|
| Новых модулей | 13 |
| Строк кода (DecisionOS-специфичных) | ~5 400 |
| Коммитов в цепочке DecisionOS | 3 (schema → foundation → docs) |
| Smoke-тестов | 8/8 passed |
| Тир-уровней | 3 (Start, Pro, Enterprise) |
| Типов аудит-событий | 5 |
| Policy engine interface | 1 (evaluate_policies) |
| Обратная совместимость | ✅ Полная (все API ROMA сохранены) |

---

## 8. Стратегическая дорожная карта (Next 90 дней)

### Q3 2026 (Week 5–8) — Production Hardening

- [ ] Stripe Checkout + CloudPayments → live payment flow
- [ ] Enterprise SSO (OIDC/SAML)
- [ ] PostgreSQL → TimescaleDB migration для audit_events (hypertable)
- [ ] SLO/SLI дашборд в Grafana (p99 latency, error rate, quota utilisation)
- [ ] Rate limiting per-tenant (token bucket)

### Q4 2026 (Week 9–16) — GTM & Scale

- [ ] Plugin marketplace MVP
- [ ] White-label onboarding flow (Enterprise tier)
- [ ] AI chat assistant для policy authoring
- [ ] Multi-region (EU, US) deployment
- [ ] SOC 2 Type I readiness assessment

---

## 9. Риски и митигация

| Риск | Вероятность | Митигация |
|------|:----------:|-----------|
| Policy engine bottleneck (single Python eval) | Средняя | Cache policy decisions per tenant (TTL 60s) |
| PostgreSQL — audit volume growth | Высокая | TimescaleDB hypertables + tier-based retention |
| Enterprise white-label — кастомизация требует per-tenant deploy | Средняя | Feature flags + tenant_config в tiers.json |
| Stripe/CloudPayments — compliance (PCI DSS) | Средняя | Stripe Elements / CloudPayments widget (токенизация на стороне провайдера) |

---

## 10. Заключение

DecisionOS v1.0.0 — это **фундамент enterprise decision infrastructure**. Платформа вышла из фазы «feature-complete executor» (ROMA) в фазу «multi-tenant, policy-gated decision platform» с чёткой тир-моделью, аудит-трейлом и обратной совместимостью. Следующий логический шаг — **production hardening** (live payments, SSO, observability) и подготовка к **GTM** (plugin marketplace, white-label onboarding, multi-region).

---

**Статус документа:** ✅ Final  
**Следующий пересмотр:** 2026-09-19 (30 дней)
