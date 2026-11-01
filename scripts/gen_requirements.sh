#!/usr/bin/env bash
# P-DEPS-LOCK (2026-11-01): requirements-*.txt — ПРОИЗВОДНЫЕ от uv.lock, руками не правятся.
#
#   bash scripts/gen_requirements.sh           # перегенерировать оба файла в корне репо
#   bash scripts/gen_requirements.sh <dir>     # записать в <dir> (так делает CI drift-check)
#
# Каноническая цепочка: pyproject.toml (прямые зависимости) → uv.lock (полный резолв) →
# requirements-ci.txt / requirements-runtime.txt (экспорт). См. docs/DEPENDENCIES.md.
# Версия uv закреплена в CI (.github/workflows/ci.yml, job deps-drift); другая версия uv
# может отформатировать экспорт иначе — тогда drift-check честно покажет разницу.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT}"
mkdir -p "$OUT"
cd "$ROOT"

# CI: рантайм + трассировка (tracing extra) + pytest-инструменты (группа dev) +
# dashboard/scripts-инструменты (группа ci). Без хешей: CI ставит `pip install -r`.
CI_CMD=(uv export --frozen --no-hashes --no-emit-project --extra tracing --group dev --group ci)
# Рантайм прода: без dev/ci-групп, с трассировкой (на Zo OTel стоит сегодня), с хешами
# для `pip install --require-hashes -r requirements-runtime.txt`.
RT_CMD=(uv export --frozen --no-emit-project --no-dev --extra tracing)

{
  echo "# GENERATED FILE — DO NOT EDIT. Source of truth: uv.lock (see docs/DEPENDENCIES.md)."
  echo "# Regenerate: bash scripts/gen_requirements.sh"
  echo "# Command:    ${CI_CMD[*]}"
  echo "# Used by:    .github/workflows/ci.yml (test, pytest-suite), security.yml (pip-audit -r)."
  "${CI_CMD[@]}" --no-header
} > "$OUT/requirements-ci.txt"

{
  echo "# GENERATED FILE — DO NOT EDIT. Source of truth: uv.lock (see docs/DEPENDENCIES.md)."
  echo "# Regenerate: bash scripts/gen_requirements.sh"
  echo "# Command:    ${RT_CMD[*]}"
  echo "# Runtime-only set for an isolated service venv (no dev/ci groups). Install:"
  echo "#   python3 -m venv <venv> && <venv>/bin/pip install --require-hashes -r requirements-runtime.txt"
  echo "# Not consumed by any automated deploy step (zo_deploy_agent does not install deps)."
  "${RT_CMD[@]}" --no-header
} > "$OUT/requirements-runtime.txt"
