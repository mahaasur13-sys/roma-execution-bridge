"""R6: негативы дрейф-проверки promtail — «регенерация платформенного конфига не проходит молча».

Класс дефекта (R6/N6): платформенный /__substrate/logging/promtail-config.yaml регенерируется
и молча теряет джобу pg_watchdog_persistent — журнал сторожа /var/lib/pg-watchdog/watchdog.log
перестаёт доходить до Loki (поток job=pg_watchdog). Прежний drift-check сверял только пары
pg-watchdog/alert-relay и барьер Grafana, поэтому дефект был невидим: фикс жил вне git под
ложным именем pre-r3fix. Детектор без сработавшего негатива считается несуществующим —
каждый негатив ниже роняет проверку на своей причине, положительный контроль не даёт
детектору «краснеть всегда».

Ловушка имени: в promtail job_name = pg_watchdog_persistent, а label в Loki = pg_watchdog.
Подмена любого из двух должна ловиться (JOB-MISSING / JOB-LABEL-DRIFT).
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DRIFT = REPO_ROOT / "scripts" / "drift_check.py"
CANON = REPO_ROOT / "deploy" / "monitoring" / "promtail-config.yaml"
SHA_FILE = REPO_ROOT / "deploy" / "monitoring" / "promtail-config.sha256"
LIVE = pathlib.Path("/__substrate/logging/promtail-config.yaml")
JOB = "pg_watchdog_persistent"
LABEL = "pg_watchdog"
PREFIX = "- job_name: pg_watchdog_persistent"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(manifest: pathlib.Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV", "COVERAGE"))}
    return subprocess.run(
        [sys.executable, str(DRIFT), "--manifest", str(manifest), "--no-alert", "--no-probe"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )


def _fixture(tmp_path: pathlib.Path, *, live: str, canon: str, fingerprint: str) -> pathlib.Path:
    """Фикстура изолирует одну причину: живой конфиг, снапшот-канон и записанный фингерпринт
    задаются независимо; пути в манифесте абсолютные, поэтому реальные файлы ноды не участвуют."""
    live_path = tmp_path / "live-promtail-config.yaml"
    canon_path = tmp_path / "canon-promtail-config.yaml"
    sha_path = tmp_path / "canon-promtail-config.sha256"
    live_path.write_text(live, encoding="utf-8")
    canon_path.write_text(canon, encoding="utf-8")
    sha_path.write_text(f"{fingerprint}  canon-promtail-config.yaml\n", encoding="utf-8")
    manifest = tmp_path / "executed_paths.json"
    manifest.write_text(
        json.dumps(
            {
                "rule": "R6 fixture: только promtail_config-проверка",
                "pairs": [],
                "checks": [
                    {
                        "name": "promtail-pg-watchdog",
                        "type": "promtail_config",
                        "live_path": str(live_path),
                        "canon_path": str(canon_path),
                        "sha256_file": str(sha_path),
                        "required_job": JOB,
                        "required_job_label": LABEL,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def _config_without_job(text: str) -> str:
    """Точное пре-фикс состояние: джоба вырезана целиком, остальное байт-в-байт."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.strip() == PREFIX)
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("- "):
        end += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    return "".join(lines[:start] + lines[end:])


def test_pre_fix_config_without_job_is_caught(tmp_path: pathlib.Path) -> None:
    """Сработавший негатив: конфиг без джобы (ровно сегодняшний дефект) — проверка падает."""
    without = _config_without_job(CANON.read_text(encoding="utf-8"))
    assert JOB not in without, "фикстура не удалила джобу"
    manifest = _fixture(
        tmp_path, live=without, canon=without, fingerprint=_sha(without)
    )
    result = _run(manifest)
    assert result.returncode == 1, f"дефект без джобы не пойман:\n{result.stdout}"
    assert "JOB-MISSING" in result.stdout
    assert "DRIFT-CHECK: FAILED" in result.stdout


def test_fingerprint_drift_is_caught(tmp_path: pathlib.Path) -> None:
    """Сработавший негатив: фингерпринт не совпадает с живым конфигом — падение."""
    text = CANON.read_text(encoding="utf-8")
    manifest = _fixture(tmp_path, live=text, canon=text, fingerprint="0" * 64)
    result = _run(manifest)
    assert result.returncode == 1, f"чужой фингерпринт не пойман:\n{result.stdout}"
    assert "FINGERPRINT-DRIFT" in result.stdout


def test_label_substitution_is_caught(tmp_path: pathlib.Path) -> None:
    """Сработавший негатив: джоба есть, но label в Loki подменён — поток не тот."""
    substituted = CANON.read_text(encoding="utf-8").replace(f"job: {LABEL}\n", "job: pg_watchdog_x\n")
    assert substituted != CANON.read_text(encoding="utf-8")
    manifest = _fixture(
        tmp_path, live=substituted, canon=substituted, fingerprint=_sha(substituted)
    )
    result = _run(manifest)
    assert result.returncode == 1, f"подмена label не поймана:\n{result.stdout}"
    assert "JOB-LABEL-DRIFT" in result.stdout


def test_live_config_matching_fingerprint_and_snapshot_passes(tmp_path: pathlib.Path) -> None:
    """Положительный контроль: корректный живой конфиг — проверка зелёная (детектор не всегда красный)."""
    text = CANON.read_text(encoding="utf-8")
    manifest = _fixture(tmp_path, live=text, canon=text, fingerprint=_sha(text))
    result = _run(manifest)
    assert result.returncode == 0, f"положительный контроль ложно краснеет:\n{result.stdout}"
    assert "DRIFT-CHECK: PASSED" in result.stdout
    assert JOB in result.stdout


def test_node_live_config_matches_committed_snapshot() -> None:
    """Инвариант ноды: живой платформенный конфиг совпадает с фингерпринтом и снапшотом в git."""
    if not LIVE.exists():
        pytest.skip(
            "issue: R6 · expiry: 2026-12-31 · платформенного promtail-конфига нет вне ноды (CI-раннер)"
        )
    result = _run(REPO_ROOT / "deploy" / "ops" / "executed_paths.json")
    assert result.returncode == 0, f"живой конфиг разошёлся с фингерпринтом/снапшотом:\n{result.stdout}"
    assert "promtail-pg-watchdog" in result.stdout


def test_repo_snapshot_and_fingerprint_agree() -> None:
    """Шина в git: снапшот и файл sha256 версионированы и согласованы между собой."""
    recorded = SHA_FILE.read_text(encoding="utf-8").split()[0]
    assert recorded == _sha(CANON.read_text(encoding="utf-8"))
    assert JOB in CANON.read_text(encoding="utf-8")
