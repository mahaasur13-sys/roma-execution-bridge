"""fix/redact-token-in-access-log: секреты не попадают в access-лог uvicorn.

Контракт:

  * ``uvicorn.access`` печатает полный путь с query-строкой, поэтому
    ``GET /auth/verify-email?token=<token>`` оставлял verify-токен в логе
    открытым текстом. Фильтр ``RedactSecretsFilter`` ставится на этот логгер
    при старте приложения (импорт ``main``) и переживает
    ``uvicorn.Config.configure_logging()`` — тот пересобирает обработчики,
    но фильтры логгера не снимает;
  * значения ``token``, ``api_key``, ``key``, ``code`` заменяются на ``***``,
    остальные параметры остаются как были;
  * проверка идёт на живом сервере: запрос уходит по сокету, запись берётся
    из настоящего обработчика uvicorn, а не собирается вручную.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn

import main
from log_redaction import RedactSecretsFilter, redact_query

SECRET = "SECRET123"


@pytest.fixture(autouse=True)
def _no_background_worker(monkeypatch):
    """Живой сервер поднимает lifespan — воркер и реконсиляция в тесте не нужны."""

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)


class _Recorder(logging.Handler):
    """Собирает форматированные строки access-лога."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_redact_query_masks_every_secret_param_and_keeps_the_rest():
    assert (
        redact_query("/auth/verify-email?token=abc&campaign=spring") == "/auth/verify-email?token=***&campaign=spring"
    )
    assert redact_query("/x?api_key=k1&key=k2&code=c3") == "/x?api_key=***&key=***&code=***"
    assert redact_query("/x?limit=10&sort=asc") == "/x?limit=10&sort=asc"
    assert redact_query("/health") == "/health"


def test_filter_is_installed_and_survives_uvicorn_logging_config():
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, RedactSecretsFilter) for f in access.filters)

    uvicorn.Config(main.app, log_config=uvicorn.config.LOGGING_CONFIG).configure_logging()

    assert any(isinstance(f, RedactSecretsFilter) for f in access.filters), (
        "dictConfig пересобирает обработчики uvicorn.access и не должен снимать фильтр"
    )
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/auth/verify-email?token={SECRET}", "1.1", 200),
        None,
    )
    access.handle(record)
    assert SECRET not in record.getMessage(), "фильтр обязан отработать и после dictConfig"
    assert "token=***" in record.getMessage()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_verify_email_request_leaves_no_token_in_access_log():
    """Живой uvicorn: GET /auth/verify-email?token=SECRET123 → в логе token=***."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="info"))

    # Обработчик ставится после Config: его конструктор пересобирает uvicorn.access.
    access = logging.getLogger("uvicorn.access")
    recorder = _Recorder()
    access.addHandler(recorder)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "сервер не поднялся за 30 с"

        url = f"http://127.0.0.1:{port}/auth/verify-email?token={SECRET}&campaign=spring"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url, timeout=10)  # noqa: S310 — loopback, свой сервер
        assert exc.value.code == 400, "запрос должен дойти до роутера (токен неизвестен → 400)"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        access.removeHandler(recorder)

    assert not thread.is_alive(), "сервер не остановился за 30 с"
    lines = [line for line in recorder.lines if "/auth/verify-email" in line]
    assert lines, f"access-лог не записал запрос: {recorder.lines}"
    text = "\n".join(lines)
    assert SECRET not in text, f"токен утёк в access-лог: {text}"
    assert "token=***" in text, f"маска не подставлена: {text}"
    assert "campaign=spring" in text, f"несекретный параметр испорчен: {text}"
