#!/usr/bin/env python3
"""Диагностический смоук GPU-интеграции — НЕ тест.

Этот файл ничего не утверждает и не участвует в pytest-коллекции (нет префикса
`test_`, вся работа — внутри main()). Раньше он лежал в корне как
`test_gpu_integration.py`: печатал литеральные «Worker state: OK» / «ALL COMPONENTS
VERIFIED» и исполнялся на уровне модуля, из-за чего падение импорта абортировало
всю коллекцию pytest (дефект R-5).

Настоящие проверки GPU-пути живут в `tests/test_gpu_integration_smoke.py`.
Запуск:
    python3 scripts/gpu_integration_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT))

    from gpu_worker.connector import get_gpu_connector, worker_url
    from scheduler.roma_scheduler import ROMAGPUScheduler

    connector = get_gpu_connector()
    metrics = connector.get_metrics()
    status = ROMAGPUScheduler().get_status()

    print("=== GPU Connector ===")
    print(f"  target URL      : {worker_url()}")
    print(f"  connector avail : {metrics['connector_available']}")
    print(
        f"  workers         : {metrics['worker_count']} (доступно {metrics['available_workers']})"
    )
    for worker in metrics["workers"]:
        print(f"    - {worker['id']}  {worker['url']}  available={worker['available']}")

    print("=== ROMA Scheduler ===")
    print(f"  execution mode  : {status['execution_mode']}")
    print(f"  gpu available   : {status['gpu_available']}")

    reachable = metrics["available_workers"] > 0
    print("=== Итог диагностики ===")
    if reachable:
        print(
            "  воркер доступен — можно проверять живой путь (ROMA_GPU_LIVE=1 pytest -m ops)"
        )
    else:
        print("  НЕТ доступного GPU-воркера: пул настроен, но /health не отвечает")
    print("  развёртывание воркера:")
    print("    docker build -f Dockerfile.gpu-worker -t roma-gpu-worker .")
    print("    docker run --gpus all -p 8000:8000 roma-gpu-worker")
    print("    ROMA_GPU_WORKER_URL=<your-server> python scheduler/roma_scheduler.py")

    return 0 if reachable else 1


if __name__ == "__main__":
    sys.exit(main())
