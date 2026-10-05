"""Контракт sidecar-адаптера Jev (эпоха G-JEV-BRIDGE, spike).

Модуль живёт вне ядра ROMA: его не импортируют ни планировщик, ни шедулер, ни
decision-gate, ни биллинг, ни раннеры. Полный контракт (state / questions /
пороги / лимиты / fallback / запреты) — `docs/spikes/G-JEV-BRIDGE.md`.

Инварианты, которые проверяются `checks/jev_checks.py`:

* секреты не покидают процесс: имена-секреты вырезаются, значения-секреты
  заменяются на `<redacted>`;
* state со неизвестными полями не отправляется «как есть»: работает allowlist;
* state сверх лимита не отправляется вовсе (skip, а не «усечём и отправим»);
* три вопроса уходят ОДНИМ вызовом system_one;
* домены payment / billing / deploy / delete / quota адаптеру запрещены;
* пороги — политика кода, не модели.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
SMOKE_MODEL = "jev-latest"

STATE_MAX_BYTES = 24 * 1024
STATE_MAX_KEYS = 40
STATE_VALUE_MAX_CHARS = 4000

STATE_ALLOWLIST = (
    "act",
    "job_id",
    "tenant_ref",
    "plan_tier",
    "queue",
    "region",
    "gpu_type",
    "priority",
    "requested_seconds",
    "est_cost_units",
    "spend_cap_remaining_units",
    "worker_availability",
    "retry_count",
    "last_failure_class",
    "deadline_seconds",
)

_KEY_DENY = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|authorization|bearer|cookie|dsn|hmac|private[_-]?key|session[_-]?id)",
    re.IGNORECASE,
)
_VALUE_DENY = re.compile(
    r"(whsec_|sk_live_|sk_test_|pk_live_|bearer\s+\S|postgres(?:ql)?://|mysql://|redis://|-----begin)",
    re.IGNORECASE,
)

FORBIDDEN_DOMAINS = ("payment", "billing", "deploy", "delete", "quota")

QUESTION_IDS = ("gate", "next", "risk")
NEXT_OPTIONS = ("run", "ask", "abort")
RISK_LEVELS = (1, 2, 3, 4, 5)

THRESHOLDS: dict[str, float] = {
    "gate_execute": 0.70,
    "gate_confirm": 0.45,
    "choice_execute": 0.60,
    "choice_confirm": 0.45,
    "risk_confirm": 3.5,
    "confidence_floor": 0.50,
}

LIMITS: dict[str, float] = {
    "timeout_s": 8.0,
    "retries": 1,
    "retry_backoff_s": 0.5,
    "max_calls_per_run": 1,
    "latency_budget_s": 12.0,
}


def tenant_ref(tenant_id: str) -> str:
    """Ссылка на тенанта вместо идентификатора: `t_` + 12 hex-символов sha256."""
    digest = hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:12]
    return f"t_{digest}"


def redact(value: Any) -> tuple[Any, list[str]]:
    """Снимает имена-секреты и значения-секреты. Возвращает (чистое, [пути])."""
    dropped: list[str] = []

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, item in node.items():
                if _KEY_DENY.search(str(key)):
                    dropped.append(f"{path}.{key}" if path else str(key))
                    continue
                out[key] = walk(item, f"{path}.{key}" if path else str(key))
            return out
        if isinstance(node, (list, tuple)):
            return [walk(item, f"{path}[{index}]") for index, item in enumerate(node)]
        if isinstance(node, str):
            if _VALUE_DENY.search(node):
                dropped.append(path or "<value>")
                return "<redacted>"
            return node
        return node

    return walk(value, ""), dropped


def build_state(raw: Any, *, state_version: int = 1) -> tuple[dict[str, Any], list[str], list[str]]:
    """Allowlist-сборка state. Возвращает (state, [пути снятых секретов], [неизвестные поля])."""
    if not isinstance(raw, dict):
        raise TypeError("state должен быть объектом")
    clean, dropped = redact(raw)
    unknown = sorted(key for key in clean if key not in STATE_ALLOWLIST)
    state: dict[str, Any] = {key: clean[key] for key in STATE_ALLOWLIST if key in clean}
    if "tenant_ref" in state:
        state["tenant_ref"] = tenant_ref(str(state["tenant_ref"]))
    state["state_version"] = state_version
    if len(state) > STATE_MAX_KEYS:
        raise ValueError("state превысил лимит полей")
    return state, dropped, unknown


def oversized_field(state: dict[str, Any]) -> str | None:
    """Первое поле, чьё значение превышает предел одного значения.

    Усечения нет осознанно: молча изменённый state — это подмена входа, а не
    экономия байтов. Не влезло — `skip`, и решение остаётся за ROMA.
    """
    for key, value in state.items():
        serialized = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(serialized) > STATE_VALUE_MAX_CHARS:
            return key
    return None


def state_bytes(state: dict[str, Any]) -> int:
    return len(json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def state_fits(state: dict[str, Any]) -> bool:
    return state_bytes(state) <= STATE_MAX_BYTES


def questions(ids: tuple[str, ...] = QUESTION_IDS) -> dict[str, dict[str, Any]]:
    """Spike-набор вопросов: gate (noul), next (choice), risk (score)."""
    catalog: dict[str, dict[str, Any]] = {
        "gate": {
            "type": "noul",
            "instructions": (
                "Данных достаточно, чтобы безопасно продолжить исполнение этой задачи без ручного разбора: "
                "очередь и регион известны, доступность воркеров не противоречива, число повторов меньше трёх."
            ),
            "criteria": {
                "true": "данных достаточно и противоречий нет",
                "false": "данных не хватает или есть противоречие",
            },
        },
        "next": {
            "type": "choice",
            "instructions": (
                "Что делать с задачей прямо сейчас, если решение принимает оператор: запустить, "
                "задать уточняющий вопрос или отказаться от исполнения."
            ),
            "options": {
                "run": "все предпосылки на месте, можно исполнять",
                "ask": "не хватает одного уточнения от оператора",
                "abort": "исполнять нельзя: противоречие или исчерпанный бюджет",
            },
        },
        "risk": {
            "type": "score",
            "instructions": (
                "Оцени риск исполнения этой задачи по фиксированной шкале от 1 до 5, где 1 — рутинная "
                "задача без отклонений, а 5 — отказ или потери."
            ),
            "levels": {
                1: "рутина, отклонений нет",
                2: "мелкие отклонения, лечится повтором",
                3: "заметный риск: нужен присмотр оператора",
                4: "высокий риск: ручной разбор до запуска",
                5: "недопустимо: запуск приведёт к отказу или потерям",
            },
        },
    }
    missing = [item for item in ids if item not in catalog]
    if missing:
        raise ValueError(f"неизвестные вопросы: {missing}")
    return {item: catalog[item] for item in ids}


def _as_mapping(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        return dict(answer)
    out: dict[str, Any] = {}
    for name in ("type", "choice", "noul", "score", "probabilities", "confidence", "legend", "options", "criteria"):
        if hasattr(answer, name):
            out[name] = getattr(answer, name)
    return out


def normalize_answers(payload: Any) -> dict[str, dict[str, Any]]:
    """Приводит ответ провайдера к одному виду для HTTP-пути и для SDK-пути."""
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("answers")
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for question_id, answer in raw.items():
        data = _as_mapping(answer)
        kind = data.get("type")
        if kind not in ("choice", "noul", "score"):
            if "choice" in data:
                kind = "choice"
            elif "noul" in data:
                kind = "noul"
            elif "score" in data:
                kind = "score"
        value = data.get(kind) if isinstance(kind, str) else None
        probabilities = data.get("probabilities")
        normalized[str(question_id)] = {
            "type": kind,
            "value": value,
            "probabilities": probabilities if isinstance(probabilities, dict) else {},
            "confidence": data.get("confidence"),
        }
    return normalized
