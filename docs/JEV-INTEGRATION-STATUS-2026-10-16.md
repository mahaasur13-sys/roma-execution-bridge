# Jev Integration — Status (ROMA) — 2026-10-16

**Статус:** зафиксировано (status-фиксация). Исторический spike-контракт —
`docs/spikes/G-JEV-BRIDGE.md`; проектные ADR'ы и дизайн — в Sentinel-репозитории
(`docs/decisions/ADD-2026-10-jev-trading-path.md`, `docs/decisions/ADD-2026-10-canonical-decision-path.md`,
`docs/jev-integration-*.md`).

## Сделано

- **Миграция шлюза** sidecar-адаптера на OpenRouter: `POST https://openrouter.ai/api/alpha/decisions`,
  модель `typesafe/jev-1.13` (пин), ключ `OPENROUTER_API_KEY`; TypeSafe SDK удалён,
  транспорт — stdlib `urllib`.
- **Промоут клиента** в `core/jev_client.py` (транспорт + ошибки + `normalize_answers` + `tenant_ref`);
  `adapters/jev/*` — тонкий backward-compat ре-экспорт + sidecar-контракт.
- **R1/R3/R5 — shadow** (read-only, off by default, fail-closed, trusted/untrusted split).
- **R3/R5 — cascade** (R3 = escalate-only, never downgrade; R5 = замена категории при confidence ≥ порога).
- **Shadow-рекордер** `core/jev_shadow_log.py` (JSONL) + **анализатор** `scripts/jev_shadow_report.py`.
- 167 проверок зелёные, Ruff чисто.

## Статус точек

| Точка | Shadow | Cascade |
|-------|--------|---------|
| R1 — decision gate anomaly | ✅ | — |
| R3 — policy / RBAC | ✅ | ✅ escalate-only |
| R5 — support-chat триаж | ✅ | ✅ |
| R2 — billing | ⏳ | — |

## Блокеры (внешние)

1. **R2** — не верифицирована подпись CloudPayments / Stripe webhook (trusted-поля суммы/статуса
   должны приходить только из подписанного источника).
2. **Пороги — заглушки** (R1: 0.90/0.70; R3: 0.85; R5: 0.90) до накопления размеченного датасета.

## Ключевые файлы (ROMA)

- `core/jev_client.py` — канонический клиент.
- `core/jev_shadow_log.py` — JSONL-рекордер shadow-наблюдений.
- `scripts/jev_shadow_report.py` — анализатор датасета.
- `cost/jev_shadow.py` — R1 harness (+ `cost/jev_shadow_checks.py`).
- `plugins/builtin/jev_policy.py` — R3 harness (+ `jev_policy_checks.py`).
- `plugins/builtin/jev_triage.py` — R5 harness (+ `jev_triage_checks.py`).
- `adapters/jev/*` — sidecar (ре-экспорт + `checks/jev_checks.py`, 121 проверка).

## Следующие шаги (операционные)

1. Включить shadow-флаги (`JEV_GATE_SHADOW`, `JEV_POLICY_SHADOW`, `JEV_TRIAGE_SHADOW`)
   в безопасном окружении → накопить `JEV_SHADOW_LOG`.
2. По датасету — пересчитать пороги (ECE < 0.05, Brier, AURC + coverage@risk, cost-of-error).
3. **R2** — после верификации подписи webhook.
