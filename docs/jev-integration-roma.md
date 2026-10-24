---
id: 20261016-jev-integration-roma
title: "Jev Integration Design — ROMA Execution Bridge"
tags:
  - project/roma
  - ai/jev
  - architecture/decision-gate
created: 2026-10-16
---

# Jev Integration Design — ROMA Execution Bridge

**Цель:** добавить Jev как System 1 слой перед существующими детерминированными гейтами без регрессии.

## Приоритетные точки интеграции

| ID | Точка | Модуль | Роль Jev | Что остаётся детерминированным |
| --- | --- | --- | --- | --- |
| R1 | Decision Gate — аномалия задания | `cost/gate.py` → `EnterpriseDecisionGate.evaluate()` | `Choice`: normal / anomaly | Quota, cost, `GateResult.ALLOWED/DENIED` |
| R2 | Биллинг — риск платежа | `billing/` + `cloudpayments_client.py` / `stripe_client.py` | `Choice`: legitimate / chargeback_risk / abuse | `pg_ledger`, finalize, debit |
| R3 | Policy / RBAC эскалация | `policy/`, `rbac/`, `tenancy/` | `Choice`: нарушение policy? | RBAC-движок как источник истины |
| R4 | Drift event-stream | `durability/`, `reconciler/` | `Score`: подозрительность расхождения | Raft + event store |
| R5 | Support-chat триаж | `support_chat/`, `alerts/` | `Choice` категории + urgency | Маршрутизация и человек |

**Порядок внедрения: R1 → R3 → R2.** R1 — самый безопасный (read-only, reversible).

## Пример R1 — аномалия задания

```python
# roma/jev_gate.py
QUESTION = {
    "type": "choice",
    "instructions": "Is this job submission consistent with the tenant's historical pattern and current plan limits?",
    "criteria": {
        "normal": "Looks consistent with tenant history and plan",
        "anomaly": "Unusual volume, pattern, or payload relative to history"
    }
}

def decide(self, tenant_id: str, payload: dict):
    state = {
        "tenant_id": tenant_id,                    # trusted
        "plan": self.db.get_plan(tenant_id),       # trusted
        "job_count_24h": self.db.count_jobs(...),  # trusted
        "gpu_required": payload.get("gpu", False),
        "payload_sha256": hashlib.sha256(...).hexdigest(),
        "untrusted_command": f"UNTRUSTED:{payload.get('command','')}"
    }
    out = self.jev.ask(QUESTION, state)

    if out.choice == "anomaly" and out.prob.get("anomaly", 0) >= self.policy.anomaly_threshold:
        return self._escalate_system2(tenant_id, payload, out)

    # normal path — существующий детерминированный гейт
    return super().evaluate(tenant_id, payload)
```

## Selective prediction (рекомендуемые стартовые пороги)

| Точка | Auto | Review | Deny (fail-closed) | Target coverage @ risk |
| --- | --- | --- | --- | --- |
| R1 | ≥ 0.90 | 0.70–0.90 | < 0.70 | ≥ 80% @ ≤ 5% risk |
| R2 | ≥ 0.95 | 0.80–0.95 | < 0.80 | ≥ 90% @ ≤ 0.5% risk |
| R3 | ≥ 0.85 | 0.65–0.85 | < 0.65 | — |

## Метрики и rollout

- Shadow mode минимум 14 дней (Jev пишет решения рядом, ничего не меняет)
- Canary: 10% tenants, только R1/R3
- Обязательные гейты: ECE < 0.05, Brier, AURC, cost-of-error, adversarial flip-rate
- Версионирование всех четырёх полей (`model_version`, `task_schema_version`, …)

## Экономика

Стоимость одного вызова Jev пренебрежимо мала относительно GPU-hours и риска chargeback.
Главный выигрыш — в R2 и в раннем отсечении аномальных заданий (R1).

> [!WARNING]
> **Ограничение.** Пока не верифицирован формат подписи CloudPayments / Stripe webhook, R2 остаётся в shadow-режиме. Trusted-поля суммы и статуса должны приходить только из подписанного источника.
