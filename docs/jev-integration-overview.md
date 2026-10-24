---
id: 20261016-jev-integration-shared
title: "Jev Integration — Shared Contract & Rules"
tags:
  - ai/jev
  - architecture/decision-layer
  - safety/selective-prediction
created: 2026-10-16
---

# Jev Integration — Shared Contract & Rules

> [!IMPORTANT]
> **Принцип.** Jev — это System 1 слой (быстрые типизированные calibrated decisions).
> Он не авторизует необратимые действия и не определяет границы своих полномочий.

## Обязательный контракт клиента

```python
# shared/jev_client.py
from dataclasses import dataclass
from typing import Literal, Dict, Any

@dataclass
class JevConfig:
    model_version: str                    # e.g. "jev-1.13.0"
    task_schema_version: str              # e.g. "gate.v1"
    threshold_policy_version: str         # e.g. "r2.v3"
    eval_dataset_version: str
    timeout_ms: int = 500
    fallback: Literal["deterministic", "deny"] = "deterministic"

@dataclass
class JevOutput:
    kind: Literal["Choice", "Score", "Noul"]
    choice: str | None = None
    score: float | None = None
    prob: Dict[str, float] = None
    confidence: float = 0.0
    raw: Dict[str, Any] = None

class JevClient:
    def ask(self, question: dict, state: dict) -> JevOutput:
        """
        Всегда возвращает типизированный ответ.
        При таймауте/ошибке — fallback согласно JevConfig.
        """
        ...
```

## Жёсткие правила (обязательны для обоих проектов)

### Трёхслойность

1. **System 2 (LLM)** — планирование, генерация гипотез, объяснения
2. **System 1 (Jev)** — выбор из заранее определённого множества
3. **Детерминированный контур** — схема, пороги, safety rules, исполнение, audit

### Trusted / Untrusted split (защита по мотивам JevAdvBench)

- **Trusted поля** — только из БД, детерминированных источников, подписанных данных
- **Untrusted поля** — payload, сырые тексты, пользовательский ввод → обязательно с префиксом `UNTRUSTED:`
- Целостность trusted-части фиксируется хешем

### Confidence ≠ авторизация

- `prob` + `confidence` + cost-of-error порог — три независимых сигнала
- Необратимые действия (debit, ордер, Raft commit, изменение весов) **никогда** не выполняются по одному ответу Jev

### Selective prediction

- Три зоны: `auto` / `review` / `deny` (fail-closed)
- Пороги выставляются по стоимости ошибки, а не по желаемой accuracy

### Версионирование всего

- `model_version` + `task_schema_version` + `threshold_policy_version` + `eval_dataset_version`
- Rollout: Shadow mode → canary → full
- Fallback всегда = существующий детерминированный путь

### Метрики перед прод

- Macro-F1 / per-class accuracy
- ECE < 0.05, Brier
- AURC + Coverage at fixed risk
- Cost of errors
- Latency p50/p99 ≤ 500 мс
- Adversarial flip-rate (JevAdvBench-style)

## Рекомендуемая структура файлов

```text
docs/
  jev-integration-overview.md
  jev-integration-roma.md
  jev-integration-astrofin.md
shared/
  jev_client.py
  jev_schemas.py
tests/
  test_jev_client.py
  test_jev_adversarial.py
```
