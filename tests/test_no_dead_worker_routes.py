"""Страж: мёртвый клиент /worker/job/* и /worker/heartbeat удалён навсегда.

На сервере ROMA нет маршрутов /worker/job/start, /worker/job/complete и
/worker/heartbeat — вызовы из них всегда получали 404. Модуль
gpu_worker/heartbeat.py удалён целиком (не переводился на worker-ack).
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
DEAD_PATHS = ("/worker/job/", "/worker/heartbeat")
# Каталоги, в которые обход не спускается: VCS, кэши инструментов, сборка и
# виртуальные окружения/вендоринг. Проектных *.py под такими именами в git нет
# (сверено с `git ls-files '*.py'`). Любой каталог с pyvenv.cfg тоже считается
# venv независимо от имени. Прочие скрытые каталоги сканируются.
PRUNE_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "site-packages",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
    }
)


def _project_py_files():
    """*.py проекта без спуска в venv/кэши (прунинг dirnames до входа в каталог)."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS and not (base / d / "pyvenv.cfg").exists())
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield base / name


def test_dead_worker_client_removed():
    assert not (ROOT / "gpu_worker" / "heartbeat.py").exists(), "gpu_worker/heartbeat.py вернулся в дерево"

    offenders = []
    for path in _project_py_files():
        if path == SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(dead in text for dead in DEAD_PATHS):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], f"обращения к несуществующим маршрутам: {offenders}"
