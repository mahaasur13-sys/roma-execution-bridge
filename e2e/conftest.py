"""Фикстура живого ROMA для Playwright smoke. Только чтение, без PG и Vast.

Запускает ``e2e/_server.py`` на эфемерном порту 127.0.0.1.
MONEY STOP: фикстура не ходит в billing/submit/refund и не передаёт секреты.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "e2e" / "_server.py"

# Переменные, которые load_env() мог бы подставить из .env через setdefault.
# Пустая строка блокирует overlay: ключ уже задан.
_BLOCKED_ENV = (
    "PG_DSN",
    "VAST_KEY",
    "VASTAI_API_KEY",
    "CLOUDPAYMENTS_PUBLIC_ID",
    "CLOUDPAYMENTS_API_SECRET",
    "CLOUDPAYMENTS_WEBHOOK_SECRET",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "SENDGRID_API_KEY",
    "DEEPSEEK_API_KEY",
    "ROMA_API_KEY",
    "JWT_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def roma_base_url(tmp_path_factory) -> str:
    tmp = tmp_path_factory.mktemp("roma-e2e")
    db_path = tmp / "roma.db"
    port = _free_port()
    env = os.environ.copy()
    for key in _BLOCKED_ENV:
        env[key] = ""
    env["VASTAI_ENABLED"] = "false"
    env["ROMA_EXECUTION_MODE"] = "local"
    env["ROMA_E2E_SQLITE"] = str(db_path)
    env["ROMA_E2E_PORT"] = str(port)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 45
    last_err: Exception | None = None
    try:
        import urllib.request

        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
                raise RuntimeError(f"uvicorn exited {proc.returncode}: {out[-2000:]}")
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except Exception as exc:  # noqa: BLE001 — ждём готовности
                last_err = exc
                time.sleep(0.2)
        else:
            raise RuntimeError(f"ROMA /health не поднялся за 45с: {last_err}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
