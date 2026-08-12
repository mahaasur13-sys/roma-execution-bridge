#!/bin/bash
# ROMA Local Deploy Script
# Usage: ./deploy.sh [--restart]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== ROMA Deploy ==="

# 1. Pull latest
echo "→ git pull"
git pull origin master

# 2. Install deps
echo "→ pip install"
source venv/bin/activate 2>/dev/null || true
pip install -e ".[dev]" -q

# 3. Compile check
echo "→ compile check"
python -m py_compile main.py && echo "  main.py OK"
python -m py_compile db.py && echo "  db.py OK"

# 4. Restart service if requested
if [[ "$1" == "--restart" ]]; then
    echo "→ restarting service"
    supervisorctl restart roma-execution-bridge
    sleep 3
    curl -sf http://localhost:8900/health && echo "  health OK" || echo "  health FAIL"
fi

echo "=== Done ==="
