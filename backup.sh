#!/bin/bash
# ROMA backup.sh — FIXED: default DB_PORT 5432→5433 (ROMA PG слушает :5433, не :5432)
# Оригинал: 1049 Б, +x, DB_PORT="${DB_PORT:-5432}" — как есть дамп падал на закрытый :5432
# Фикс авторизован: GO fix-backup-sh (danger-full-access escalation)
# Применение: владельцем C в ~/roma-execution-bridge, не из DSH

set -euo pipefail

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5433}"
DB_USER="${DB_USER:-roma}"
DB_PASSWORD="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-roma}"
BACKUP_DIR="${BACKUP_DIR:-/backup/postgres}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/roma_${TIMESTAMP}.sql.gz"
LOG_FILE="${BACKUP_DIR}/backup.log"

mkdir -p "${BACKUP_DIR}"

echo "[$(date -Is)] Starting backup: ${DB_HOST}:${DB_PORT}/${DB_NAME} -> ${BACKUP_FILE}" | tee -a "${LOG_FILE}"

# pg_dump с явным портом 5433 по умолчанию, но override через env DB_PORT возможен
PGPASSWORD="${DB_PASSWORD}" pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" --no-owner --no-acl | gzip > "${BACKUP_FILE}"

echo "[$(date -Is)] Backup OK: ${BACKUP_FILE} ($(du -h ${BACKUP_FILE} | cut -f1))" | tee -a "${LOG_FILE}"

# retention 7 дней
find "${BACKUP_DIR}" -name "roma_*.sql.gz" -type f -mtime +${RETENTION_DAYS} -delete
echo "[$(date -Is)] Cleanup old >${RETENTION_DAYS}d done" | tee -a "${LOG_FILE}"
