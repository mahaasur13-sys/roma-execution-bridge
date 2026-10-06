"""P1-C guard: импорт модулей приложения не должен менять os.environ.

Грабли Г5: `PG_DSN` оказывался в окружении только потому, что кто-то импортировал
`main.py`, который на уровне модуля вызывал `load_env()`. Из-за этого результат
теста зависел от порядка импортов.

Проверка идёт в отдельном процессе: иначе кэш модулей сделал бы её бессмысленной
(модули уже импортированы более ранними тестами).

Исключения (осознанные, issue: P1-C2):
  * `env_loader` — сам модуль загрузки конфига, ему менять окружение положено.
  * `main` — точка входа приложения: она обязана загрузить конфиг до импорта
    потребителей DSN. Перевод DSN на ленивое чтение — отдельный трек.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

MODULES = [
    "db_adapter",
    "billing.pg_connection",
    "cost.gate",
    "policy_engine",
    "router_jobs",
]

PROBE = r'''
import json, os, sys

mods = json.loads(sys.argv[1])
before = dict(os.environ)
import importlib

for name in mods:
    importlib.import_module(name)
after = dict(os.environ)

added = sorted(set(after) - set(before))
removed = sorted(set(before) - set(after))
changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
print(json.dumps({"added": added, "removed": removed, "changed": changed}))
'''


def test_import_does_not_mutate_env() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_root
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(MODULES)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr[-800:]}"
    diff = json.loads(proc.stdout.strip().splitlines()[-1])
    assert diff["added"] == [], f"импорт добавил env-переменные: {diff['added']}"
    assert diff["removed"] == [], f"импорт удалил env-переменные: {diff['removed']}"
    assert diff["changed"] == [], f"импорт изменил env-переменные: {diff['changed']}"
