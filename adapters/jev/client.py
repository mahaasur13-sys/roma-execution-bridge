"""Транспорт адаптера: один вызов `system_one`, ключ только из env, ключ не логируется.

Правила эпохи, зашитые здесь:

* сеть возможна только когда одновременно `JEV_ENABLED` включён И
  `TYPESAFE_API_KEY` непустой — иначе no-op (никакого сокета);
* endpoint только официальный: `POST https://api.typesafe.ai/v1/systemone`;
* ключ берётся только из env процесса; проброс клиентского ключа, ключи
  «фермерских» аккаунтов, временная почта и сторонние прокси запрещены;
* ключ и тела ошибок маскируются перед попаданием в текст исключения;
* повтор — только на 429/529 и только один (retries=1): таймаут не повторяем,
  иначе latency-бюджет тратится дважды;
* предпочтителен официальный SDK (`typesafe_sdk.TypeSafeClient.system_one`),
  если он установлен; иначе — stdlib-запрос на тот же endpoint. SDK не
  обязателен для установки и запуска ROMA и не входит в её зависимости.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from adapters.jev.contract import DEFAULT_MODEL, ENDPOINT, LIMITS

_RETRY_STATUSES = (429, 529)
_SECRET_IN_TEXT = re.compile(
    r"(whsec_\w+|sk_(?:live|test)_\w+|pk_live_\w+|bearer\s+\S+|postgres(?:ql)?://\S+|redis://\S+)",
    re.IGNORECASE,
)


class JevError(RuntimeError):
    """Базовая ошибка адаптера: наружу её гасит adapter.decide -> skip."""


class JevDisabled(JevError):
    """Флаг выключен или ключа нет — сеть не трогаем."""


class JevTimeout(JevError):
    """Провайдер не ответил в таймаут."""


class JevRateLimited(JevError):
    """429/529 после исчерпания повторов."""


class JevBadResponse(JevError):
    """Ответ не разобран: не JSON, неожиданный статус, битая форма, чужой тип."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект не исполняем: официальный endpoint отвечает сам, а 3xx уводит
    запрос вместе с Bearer-ключом на произвольный хост."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - подпись urllib
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def mask(text: Any, key: str = "", limit: int = 300) -> str:
    """Маскирует секреты в тексте и режет длину — для сообщений об ошибках.

    Кроме шаблонов (`whsec_…`, `sk_live_…`, `Bearer …`, DSN) вырезается литерал
    ключа, если он передан: провайдер может вернуть ключ в теле без слова
    `Bearer` (например `{"key": "…"}`), и шаблон такой ключ не поймает.
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
    return _truthy(source.get("JEV_ENABLED")) and bool(str(source.get("TYPESAFE_API_KEY", "")).strip())


def api_key(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return str(source.get("TYPESAFE_API_KEY", "")).strip()


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


def _sdk_module() -> Any | None:
    try:
        import typesafe_sdk  # type: ignore[import-not-found]
    except Exception:
        return None
    return typesafe_sdk


def _sdk_available() -> bool:
    return _sdk_module() is not None


class JevClient:
    """Клиент одного вызова system_one. Ключ не хранится в открытом виде в текстах ошибок."""

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
        if _sdk_available():
            return self._sdk_transport
        return http_transport

    def _sdk_transport(self, url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
        """Путь официального SDK. Не проверен против живого сервиса в этой эпохе."""
        module = _sdk_module()
        if module is None:
            return http_transport(url, payload, key, timeout)
        try:
            client = module.TypeSafeClient(api_key=key)
            try:
                response = client.system_one(
                    state=payload.get("state"), questions=payload.get("questions"), model=self.model
                )
            except TypeError:
                response = client.system_one(state=payload.get("state"), questions=payload.get("questions"))
        except Exception as exc:
            raise JevError(f"SDK: {mask(exc, key)}") from exc
        if isinstance(response, dict):
            return response
        answers = getattr(response, "answers", None)
        return {"answers": answers if isinstance(answers, dict) else {}, "model": getattr(response, "model", None)}

    def evaluate(self, state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Одна оценка system_one. Исключения наружу гасит adapter.decide -> skip."""
        if not self.enabled:
            raise JevDisabled("JEV_ENABLED выключен или TYPESAFE_API_KEY пуст")
        payload = {"state": state, "model": self.model, "questions": questions}
        key = api_key(self._env)
        delay = self.backoff
        last: JevError | None = None
        for attempt in range(self.retries + 1):
            self.attempts += 1
            try:
                return self._transport(ENDPOINT, payload, key, self.timeout)
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
        raise last if last is not None else JevError("transport exhausted")
