#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
DSN=$(grep -E '^PG_DSN=' .env | cut -d= -f2- | tr -d '"' | tr -d "'" | xargs)
CRED="${DSN#*://}"; CRED="${CRED%@*}"
export DB_USER="${CRED%%:*}"
export DB_PASSWORD="${CRED#*:}"
HOSTPORT="${DSN#*@}"; HOSTPORT="${HOSTPORT%%/*}"
export DB_HOST="${HOSTPORT%%:*}"
export DB_PORT="${HOSTPORT#*:}"; DB_PORT="${DB_PORT%%/*}"
export DB_NAME="${DSN##*/}"
export BACKUP_DIR="/home/felix/backups/roma"
./backup.sh
