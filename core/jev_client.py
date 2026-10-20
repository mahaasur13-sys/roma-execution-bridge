"""core/jev_client.py — канонический Jev-клиент (OpenRouter Decisions API).

Единственный источник транспорта / разбора / де-идентификации для всех
потребителей Jev в ROMA (``cost/``, ``plugins/builtin/`` и sidecar ``adapters/jev/``).

Правила:
    * сеть возможна только когда одновременно ``JEV_ENABLED`` включён И
      ``OPENROUTER_API_KEY`` непустой — иначе no-op (никакого сокета);
    * endpoint только официальный: ``POST https://openrouter.ai/api/alpha/decisions``;
    * ключ берётся только из env процесса; ключ и тела ошибок маскируются;
    * повтор — только на 429/529 и только один (retries=1): таймаут не повторяем;
    * транспорт — только stdlib (``urllib``); SDK не используется;
    * модель закреплена: ``typesafe/jev-1.13`` (``jev-latest`` запрещён).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# ── Конфигурация транспорта ─────────────────────────────────────────────

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
PINNED_JEV_MODEL = "typesafe/jev-1.13"
FORBIDDEN_MODEL_MARKERS = ("jev-latest",)
DEFAULT_MODEL = PINNED_JEV_MODEL

LIMITS: dict[str, float] = {
    "timeout_s": 8.0,
    "retries": 1,
    "retry_backoff_s": 0.5,
    "max_calls_per_run": 1,
    "latency_budget_s": 12.0,
}

# ── Ошибки ───────────────────────────────────────────────────────────────


class JevError(RuntimeError):
    """Базовая ошибка адаптера: наружу её гасит adapter.decide -> skip."""

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class JevDisabled(JevError):
    """Флаг выключен или ключа нет — сеть не трогаем."""


class JevTimeout(JevError):
    """Провайдер не ответил в таймаут."""


class JevRateLimited(JevError):
    """429/529 после исчерпания повторов."""


class JevBadResponse(JevError):
    """Ответ не разобран: не JSON, неожиданный статус, битая форма, чужой тип."""


# ── Транспорт ────────────────────────────────────────────────────────────

_RETRY_STATUSES = (429, 529)
_SECRET_IN_TEXT = re.compile(
    r"(whsec_\w+|sk_(?:live|test)_\w+|pk_live_\w+|bearer\s+\S+|postgres(?:ql)?://\S+|redis://\S+)",
    re.IGNORECASE,
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект не исполняем: официальный endpoint отвечает сам, а 3xx уводит
    запрос вместе с Bearer-ключом на произвольный хост."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - подпись urllib
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def mask(text: Any, key: str = "", limit: int = 300) -> str:
    """Маскирует секреты в тексте и режет длину — для сообщений об ошибках.

    Кроме шаблонов (``whsec_…``, ``sk_live_…``, ``Bearer …``, DSN) вырезается литерал
    ключа, если он передан: провайдер может вернуть ключ в теле без слова
    ``Bearer`` (например ``{"key": "…"}``), и шаблон такой ключ не поймает.
    """
    text = str(text)
    if key:
        for variant in (key, urllib.parse.quote(key, safe="")):
            if len(variant) >= 8:
                text = text.replace(variant, "<redacted>")
    return _SECRET_IN_TEXT.sub("<redacted>", text)[:limit]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled(env: dict[str, str] | None = None) -> bool:
    """Сеть разрешена только при включённом флаге И непустом ключе."""
    source = os.environ if env is None else env
    return _truthy(source.get("JEV_ENABLED")) and bool(str(source.get("OPENROUTER_API_KEY", "")).strip())


def api_key(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return str(source.get("OPENROUTER_API_KEY", "")).strip()


def http_transport(url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    """Официальный HTTP-путь: POST с Bearer-ключом."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:  # noqa: S310 - официальный https-endpoint
            content_type = response.headers.get_content_type()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise JevBadResponse(f"content-type: {content_type}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = mask(exc.read().decode("utf-8", "replace"), key)
        if exc.code in _RETRY_STATUSES:
            raise JevRateLimited(f"HTTP {exc.code}: {detail}") from exc
        if 300 <= exc.code < 400:
            raise JevBadResponse(f"HTTP {exc.code}: редирект запрещён") from exc
        raise JevBadResponse(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        text = mask(exc, key)
        if isinstance(exc, TimeoutError) or "timed out" in text.lower():
            raise JevTimeout(f"timeout: {text}") from exc
        raise JevError(f"transport: {text}") from exc
    except json.JSONDecodeError as exc:
        raise JevBadResponse("ответ не JSON") from exc


# ── Пин модели ──────────────────────────────────────────────────────────


def check_model_pin(model: str) -> None:
    """Модель закреплена: чужая (включая ``jev-latest``) — отказ до сети."""
    if model != PINNED_JEV_MODEL or any(marker in model for marker in FORBIDDEN_MODEL_MARKERS):
        raise JevError(
            f"модель Jev закреплена как {PINNED_JEV_MODEL!r}, получено {model!r}",
            reason="model_pin",
        )


def check_response_model(payload: Any) -> None:
    """Модель в ответе: поле отсутствует — ок; есть и не совпало — отказ.

    OpenRouter может вернуть датированный снапшот той же модели
    (``typesafe/jev-1.13-20260917``) — принимаем его как тот же пин;
    иная модель / префикс без 8-значной даты / чужой неймспейс — отказ.
    """
    if not isinstance(payload, dict):
        return
    reported = payload.get("model")
    if reported is None or str(reported) == PINNED_JEV_MODEL:
        return
    if re.fullmatch(rf"{re.escape(PINNED_JEV_MODEL)}-[0-9]{{8}}", str(reported)):
        return
    raise JevError(f"ответ Jev от незакреплённой модели: {reported!r}", reason="model_pin_response")


# ── Клиент ───────────────────────────────────────────────────────────────


class JevClient:
    """Клиент одного вызова Decisions API. Ключ не хранится в открытом виде в текстах ошибок."""

    def __init__(
        self,
        env: dict[str, str] | None = None,
        transport: Callable[[str, dict[str, Any], str, float], dict[str, Any]] | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float | None = None,
        retries: int | None = None,
        backoff: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._env = dict(os.environ if env is None else env)
        check_model_pin(model)
        self.model = model
        self.timeout = float(LIMITS["timeout_s"] if timeout is None else timeout)
        self.retries = int(LIMITS["retries"] if retries is None else retries)
        self.backoff = float(LIMITS["retry_backoff_s"] if backoff is None else backoff)
        self._sleep = sleep
        self._transport = transport if transport is not None else self._default_transport()
        self.attempts = 0

    @property
    def enabled(self) -> bool:
        return is_enabled(self._env)

    @property
    def api_key(self) -> str:
        """Ключ из env — нужен только чтобы вырезать его из текстов ошибок."""
        return api_key(self._env)

    def _default_transport(self) -> Callable[[str, dict[str, Any], str, float], dict[str, Any]]:
        return http_transport

    def evaluate(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Одна оценка Decisions API. Исключения наружу гасит adapter.decide -> skip."""
        check_model_pin(self.model)
        if not self.enabled:
            raise JevDisabled("JEV_ENABLED выключен или OPENROUTER_API_KEY пуст")
        payload = {"state": state, "model": self.model, "questions": questions}
        key = api_key(self._env)
        delay = self.backoff
        last: JevError | None = None
        for attempt in range(self.retries + 1):
            self.attempts += 1
            try:
                response = self._transport(ENDPOINT, payload, key, self.timeout)
            except JevRateLimited as exc:
                last = exc
                if attempt < self.retries:
                    self._sleep(delay)
                    delay *= 2
                    continue
                raise
            except JevError as exc:
                raise type(exc)(mask(str(exc), key)) from exc
            except Exception as exc:
                raise JevError(f"transport: {mask(exc, key)}") from exc
            check_response_model(response)
            return response
        raise last if last is not None else JevError("transport exhausted")


# ── Де-идентификация и разбор ответа ─────────────────────────────────────


def tenant_ref(tenant_id: str) -> str:
    """Ссылка на тенанта вместо идентификатора: ``t_`` + 12 hex-символов sha256."""
    digest = hashlib.sha256(str(tenant_id).encode("utf-8")).hexdigest()[:12]
    return f"t_{digest}"


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


__all__ = [
    "ENDPOINT",
    "DEFAULT_MODEL",
    "LIMITS",
    "JevError",
    "JevDisabled",
    "JevTimeout",
    "JevRateLimited",
    "JevBadResponse",
    "JevClient",
    "mask",
    "is_enabled",
    "api_key",
    "http_transport",
    "tenant_ref",
    "normalize_answers",
]
