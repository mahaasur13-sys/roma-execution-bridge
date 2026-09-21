"""R-5: GPU-интеграция — настоящие проверки вместо литеральных «OK».

История: до 2026-09-21 это был корневой `test_gpu_integration.py`, который печатал
литеральные строки («Worker state: OK», «✅ ALL COMPONENTS VERIFIED»), исполнял
импорт, конструкторы и разбор env НА УРОВНЕ МОДУЛЯ, и падал ещё на импорте
(IndexError в connector.py при пустом ROMA_GPU_WORKERS) — то есть абортировал ВСЮ
коллекцию pytest, не проверив ничего.

Здесь три уровня честности:
  1) инварианты, обязанные держаться БЕЗ GPU и БЕЗ env — исполняются всегда;
  2) путь исполнителя (route_job) — обычная проверка контракта решения; дефект P1-B
     закрыт 2026-09-21 (evaluate(tenant_id, payload) по фактической сигнатуре);
  3) живой GPU-воркер — только по явному ROMA_GPU_LIVE=1, иначе skip с reason+issue.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = Path(__file__).resolve().parents[1]

GPU_ENV_VARS = (
    "ROMA_GPU_WORKER_URL",
    "ROMA_GPU_WORKERS",
    "ROMA_GPU_TIMEOUT",
    "GPU_POOL_DISCOVERY",
)

ISSUE_R5 = "R-5"

GPU_JOB = {
    "job_id": "test-gpu-001",
    "task_type": "ml_training",
    "command": "nvidia-smi --query-gpu=name --format=csv,noheader",
    "gpu_required": True,
    "memory": "8GB",
    "timeout": 30,
    "tenant_tier": "PRO",
    "plan_tier": "PRO",
    "tenant_id": "tenant_probe",
}


def _clean_env(**overrides: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in GPU_ENV_VARS}
    env.update(overrides)
    return env


def _run(code: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_modules_import_without_gpu_or_env() -> None:
    """Импорт не зависит от окружения (грабли Г5): никакого разбора env внутри импорта."""
    proc = _run(
        "import gpu_worker.connector, scheduler.roma_scheduler; print('imported')",
        _clean_env(),
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "imported" in proc.stdout


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8000", "worker-localhost"),
        ("http://10.0.0.5:8000", "worker-10.0.0.5"),
        ("gpu.example.com:8765", "worker-gpu.example.com"),
        ("https://gpu.example.com", "worker-gpu.example.com"),
    ],
)
def test_worker_id_parsing_is_total(url: str, expected: str) -> None:
    """Разбор воркера не падает на форме без схемы и не режет host по ':' вслепую."""
    from gpu_worker.connector import worker_id_for

    assert worker_id_for(url) == expected


def test_worker_id_parsing_survives_garbage() -> None:
    from gpu_worker.connector import worker_id_for

    for garbage in ("", " ", "not a url", "://"):
        assert worker_id_for(garbage).startswith("worker-")


def test_worker_pool_treats_blank_env_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустое значение ROMA_GPU_WORKERS = «не задано», а не «ноль воркеров».

    Именно на этом падал сбор: `os.getenv("ROMA_GPU_WORKERS", default)` возвращает
    '' для строки вида `ROMA_GPU_WORKERS=` в .env, и пул молча становился пустым.
    """
    import gpu_worker.connector as connector

    # (а) пусто и там, и там → дефолтный URL, пул не пуст
    monkeypatch.setenv("ROMA_GPU_WORKERS", "")
    monkeypatch.delenv("ROMA_GPU_WORKER_URL", raising=False)
    pool = connector.GPUWorkerPool()
    assert len(pool.workers) == 1
    assert pool.workers[0]["url"] == connector.DEFAULT_WORKER_URL

    # (б) пустой список воркеров, но заданный URL → берётся URL, а не ноль
    monkeypatch.setenv("ROMA_GPU_WORKERS", "")
    monkeypatch.setenv("ROMA_GPU_WORKER_URL", "http://gpu-host:8765")
    pool2 = connector.GPUWorkerPool()
    assert [w["url"] for w in pool2.workers] == ["http://gpu-host:8765"]


def test_bad_timeout_fails_at_use_not_at_import() -> None:
    """Битый ROMA_GPU_TIMEOUT: импорт проходит, ошибка — в точке использования."""
    proc_int = _run(
        "import gpu_worker.connector; print('imported')",
        _clean_env(ROMA_GPU_TIMEOUT="not-a-number"),
    )
    assert proc_int.returncode == 0, proc_int.stderr[-500:]

    proc_use = _run(
        "import gpu_worker.connector as c; c.gpu_timeout()",
        _clean_env(ROMA_GPU_TIMEOUT="not-a-number"),
    )
    assert proc_use.returncode != 0
    assert "ROMA_GPU_TIMEOUT" in proc_use.stderr


def test_scheduler_status_contract() -> None:
    """Публичный контракт статуса: ключи и типы, без GPU."""
    from scheduler.roma_scheduler import ROMAGPUScheduler

    status = ROMAGPUScheduler().get_status()
    assert "execution_mode" in status
    assert isinstance(status["gpu_available"], bool)


def test_cost_gate_contract_holds() -> None:
    """Реальная сигнатура гейта: (tenant_id, payload)."""
    from cost.gate import EnterpriseDecisionGate, GateDecision

    decision = EnterpriseDecisionGate().evaluate(tenant_id="tenant_probe")
    assert isinstance(decision, GateDecision)
    assert decision.tenant_id == "tenant_probe"
    assert decision.reason


def test_job_submit_path_returns_route() -> None:
    """Документированный путь submit обязан вернуть маршрут, а не исключение."""
    import asyncio
    import logging

    from scheduler.roma_scheduler import ROMAJobExecutor

    logging.disable(logging.CRITICAL)

    job = dict(GPU_JOB)
    job["gpu_required"] = False
    route = asyncio.run(ROMAJobExecutor().submit(job))
    assert isinstance(route, dict) and route.get("status")


@pytest.mark.skipif(
    not os.environ.get("ROMA_GPU_LIVE"),
    reason=f"живой GPU-воркер: нужен ROMA_GPU_LIVE=1 (issue: {ISSUE_R5} · expiry: 2026-12-31)",
)
def test_live_gpu_worker_health() -> None:
    """Смоук против ЖИВОГО воркера — только по явному opt-in."""
    import requests

    from gpu_worker.connector import get_gpu_connector

    connector = get_gpu_connector()
    metrics = connector.get_metrics()
    assert metrics["worker_count"] >= 1

    url = connector.pool.workers[0]["url"]
    resp = requests.get(f"{url}/health", timeout=5)
    assert resp.status_code == 200
