"""Страж: мёртвый клиент /worker/job/* и /worker/heartbeat удалён навсегда.

На сервере ROMA нет маршрутов /worker/job/start, /worker/job/complete и
/worker/heartbeat — вызовы из них всегда получали 404. Модуль
gpu_worker/heartbeat.py удалён целиком (не переводился на worker-ack).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
DEAD_PATHS = ("/worker/job/", "/worker/heartbeat")


def test_dead_worker_client_removed():
    assert not (ROOT / "gpu_worker" / "heartbeat.py").exists(), "gpu_worker/heartbeat.py вернулся в дерево"

    offenders = []
    for path in ROOT.rglob("*.py"):
        if path == SELF or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(dead in text for dead in DEAD_PATHS):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], f"обращения к несуществующим маршрутам: {offenders}"
