# ROMA Scheduler & Cost-Gate Audit — 2026-08-15

**Scope:** `scheduler/` (7 файлов), `billing/pricing_engine.py`, `cost/` (gate + predictor)  
**Methodology:** полный обход всех модулей, проверка импортов, потоков данных, конкурентности  
**Findings:** 3 P0 · 4 P1 · 3 P2 · 2 позитивных

---

## P0 — Критические (3)

### P0-1: `roma_scheduler.py:97` — nested `asyncio.run()` в `run_in_executor()`

```python
# Строка 97
async def run():
    return await self.scheduler.execute_job(job)
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, lambda: asyncio.run(run()))
```

**Проблема:** `asyncio.run()` создаёт новый event loop внутри существующего — вызовет `RuntimeError: This event loop is already running` при любом асинхронном GPU job.

**Риск:** GPU-задачи никогда не выполняются — падают с исключением.

**Исправление:**
```python
# Прямой await вместо run_in_executor
result = await self.scheduler.execute_job(job)
```

---

### P0-2: `gpu_scheduler.py:26` — VRAM tracking полностью сломан

```python
class GPUScheduler:
    def __init__(self, queue_manager: QueueManager):
        self._vram_used_mb = 0  # ← всегда 0

    def release_vram(self, job_id: str):
        pass  # ← no-op, ничего не освобождает
```

**Проблема:** `_vram_used_mb` инициализирован 0 и нигде не обновляется. `can_schedule()` всегда возвращает True. `release_vram()` — пустой метод.

**Риск:** GPU-задачи никогда не блокируются по VRAM. При переподписке — OOM на GPU-ноде.

**Исправление:** Заменить `GPUScheduler` на `GPUPolicyEngineV2` (`gpu_policy_engine_v2.py`, 328 строк), который уже реализует:
- Multi-node VRAM tracking с allocate/release
- Backpressure с saturation threshold (85%)
- Priority aging
- Batch size auto-tuning
- Gang scheduling

V2 engine существует и полностью протестирован, но **не подключен** в `roma_scheduler.py`.

---

### P0-3: `pricing_engine.py:30` — `estimate_cost()` жёстко использует PRO-тариф

```python
def estimate_cost(tenant_id: str, ...):
    pe = PricingEngine()
    result = pe.calculate(PricingTier.PRO, ...)  # ← всегда PRO
    return result["total"]
```

**Проблема:** FREE и ENTERPRISE тенанты всегда костятся по PRO-тарифу. `tenant_id` передаётся, но игнорируется.

**Риск:** Некорректный биллинг для всех tier'ов кроме PRO.

**Исправление:** Запрашивать tier у `TenantManager`:
```python
from tenancy.manager import TenantManager
tm = TenantManager()
tier_str = tm.get_tenant_info(tenant_id).get("plan", "FREE").upper()
tier_enum = PricingTier[tier_str] if tier_str in PricingTier.__members__ else PricingTier.FREE
result = pe.calculate(tier_enum, ...)
```

---

## P1 — Высокие (4)

### P1-1: `roma_scheduler.py:116` — глобальный singleton без tenant-изоляции

```python
_executor: Optional[ROMAJobExecutor] = None

def get_executor() -> ROMAJobExecutor:
    global _executor
    if _executor is None:
        _executor = ROMAJobExecutor()
    return _executor
```

**Проблема:** Единственный экземпляр `ROMAJobExecutor` на всех tenant'ов. `self.results: dict` смешивает результаты разных tenant'ов.

**Риск:** Утечка данных между tenant'ами через `get_result()`.

**Исправление:** Заменить на `Dict[str, ROMAJobExecutor]` с ключом `tenant_id`:
```python
_executors: Dict[str, ROMAJobExecutor] = {}

def get_executor(tenant_id: str) -> ROMAJobExecutor:
    if tenant_id not in _executors:
        _executors[tenant_id] = ROMAJobExecutor()
    return _executors[tenant_id]
```

---

### P1-2: `gpu_scheduler.py:58` — доступ к недокументированным атрибутам QueueManager

```python
def snapshot(self) -> dict:
    return {
        "gpu_busy": self.queue.is_gpu_busy,   # ← не документировано
        "queue_depth": self.queue.queue_depth,  # ← может быть методом
    }
```

**Проблема:** Прямой доступ к `is_gpu_busy` и `queue_depth` как атрибутам. Если QueueManager использует методы/свойства — AttributeError.

**Риск:** `snapshot()` падает при вызове.

**Исправление:** Согласовать интерфейс с QueueManager или обернуть в try/except.

---

### P1-3: `roma_scheduler.py:35` — `DecisionGate()` без обработки ошибок

```python
self.cost_gate = DecisionGate()  # ← нет try/except
```

**Проблема:** Конструктор `DecisionGate()` создаёт `TenantManager()` — если tenancy не настроен, весь scheduler падает при инициализации.

**Риск:** Scheduler не стартует при отсутствии tenancy-конфига.

**Исправление:** Обернуть в try/except с graceful degradation:
```python
try:
    self.cost_gate = DecisionGate()
except Exception as e:
    logger.warning("DecisionGate init failed: %s, cost gate disabled", e)
    self.cost_gate = None
```

---

### P1-4: `cost/predictor.py:51` — `gpu_node` жёстко закодирован

```python
gpu_node = "gpu-node-1" if gpu_required else "cpu-cluster"
gpu_count = 1 if gpu_required else 0
```

**Проблема:** Не интегрирован с `GPUPolicyEngineV2`, который знает реальную топологию кластера (multi-node, VRAM per node).

**Риск:** Предсказания стоимости не учитывают реальное размещение на нодах. Если gpu-node-1 занята, cost estimate всё равно ссылается на неё.

**Исправление:** Интегрировать `CostPredictor` с `GPUPolicyEngineV2`:
```python
from scheduler.gpu_policy_engine_v2 import GPUPolicyEngineV2
node = self.policy_engine.select_best_node(vram_gb=...)
gpu_node = node.name if node else "no-gpu-available"
```

---

## P2 — Средние (3)

### P2-1: `instance_scheduler.py:40` — `asyncio.create_task()` без ссылки

```python
asyncio.create_task(assign_job_to_worker(...))  # ← fire-and-forget
```

**Проблема:** Задача создаётся, но не сохраняется в переменную. Если WebSocket send падает — ошибка теряется, job теряется.

**Исправление:**
```python
task = asyncio.create_task(assign_job_to_worker(...))
task.add_done_callback(lambda t: logger.error("WS error: %s", t.exception()) if t.exception() else None)
```

---

### P2-2: `pricing_engine.py:8` — `estimate_duration()` — хрупкий keyword-matching

```python
if any(kw in task.lower() for kw in ["yolo", "detection", "train"]):
    return base * 4
```

**Проблема:** Субстринг-матчинг ловит ложные совпадения: "hullo" → содержит "yolo"? Нет, но "yolov5-pretrained" → да. Классификатор не различает intent.

**Исправление:** Использовать enum-based task categories вместо keyword matching:
```python
from enum import Enum
class TaskCategory(Enum):
    CV_TRAINING = "cv_training"      # base * 4
    LLM_INFERENCE = "llm_inference"  # base * 8
    IMAGE_GEN = "image_gen"          # base * 6
    DEFAULT = "default"              # base * 1
```

---

### P2-3: `cost/gate.py:112` — `_gate_message()` внедряет `$` для всех валют

```python
return f"cost ${cost:.4f} exceeds {tier} plan limit ${limit:.2f}"
```

**Проблема:** Хардкод `$`. Если добавится EUR/RUB — сообщение будет вводить в заблуждение.

**Исправление:** Использовать поле `currency` из prediction breakdown.

---

## Позитивные находки (2)

### + GPUPolicyEngineV2 (`gpu_policy_engine_v2.py`, 328 строк)

**Отлично.** Полноценный GPU-планировщик с:
- Multi-node VRAM tracking (allocate/release)
- Backpressure с saturation threshold (85%)
- Priority aging с fair-share weighting
- Batch size auto-tuning per node
- Gang scheduling (multi-GPU jobs)
- Полная статистика кластера

**Проблема:** Не подключен в `roma_scheduler.py`. `GPUScheduler` (63 строки, сломан) используется вместо `GPUPolicyEngineV2` (328 строк, рабочий).

### + DecisionGate (`cost/gate.py`, 198 строк)

**Хорошо.** Чистая архитектура:
- Tier limits (FREE: $1, PRO: $50, ENTERPRISE: $500)
- Стандартизированный GateResponse
- История решений по tenant'ам
- Правильная обработка $0.00 как REQUIRES_CONFIRMATION
- Graceful error handling

---

## Интеграционные разрывы

```
GPUPolicyEngineV2 (рабочий) ──не подключен──→ roma_scheduler.py
                                                 │
                                                 ├──→ GPUScheduler (сломан)
                                                 ├──→ DecisionGate ✅
                                                 ├──→ CostPredictor (без топологии)
                                                 └──→ ROMAJobExecutor (без tenant-изоляции)

CostPredictor.gpu_node = "gpu-node-1" (hardcoded)
    ↕ не интегрирован
GPUPolicyEngineV2.select_best_node() (знает реальную топологию)
```

---

## Сводка рекомендаций

| # | Приоритет | Файл | Действие | Время |
|---|-----------|------|----------|-------|
| 1 | P0 | `scheduler/roma_scheduler.py` | Заменить `GPUScheduler` → `GPUPolicyEngineV2` | 30 мин |
| 2 | P0 | `scheduler/roma_scheduler.py` | Убрать `asyncio.run()` в `submit()` | 5 мин |
| 3 | P0 | `billing/pricing_engine.py` | `estimate_cost()` → учитывать tenant tier | 10 мин |
| 4 | P1 | `scheduler/roma_scheduler.py` | Tenant-изоляция для `ROMAJobExecutor` | 20 мин |
| 5 | P1 | `cost/predictor.py` | Интегрировать `select_best_node()` из v2 | 15 мин |
| 6 | P1 | `scheduler/roma_scheduler.py` | Обработка ошибок `DecisionGate()` | 5 мин |
| 7 | P2 | `pricing_engine.py` | TaskCategory enum вместо keyword matching | 20 мин |

---

## GPU Efficiency Audit — вердикт

**Архитектура правильная** (cost-aware scheduling, VRAM tracking, backpressure), но:
- **Реализация отстаёт от архитектуры** — v2 engine написан, но не подключен
- **VRAM enforcement не работает** — `gpu_scheduler.py` всегда разрешает все задачи
- **Стоимость некорректна** — `estimate_cost()` игнорирует tenant tier

После подключения v2 engine и исправления P0-1/P0-3 система будет готова к GPU Efficiency Audit с реальными метриками DCGM.
