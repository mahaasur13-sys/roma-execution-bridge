#!/bin/bash
set -euo pipefail
DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5433}"
DB_USER="${DB_USER:-roma}"
DB_PASSWORD="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-roma}"
BACKUP_DIR="${BACKUP_DIR:-/home/felix/backups/roma}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/roma_${TIMESTAMP}.sql.gz"
LOG_FILE="${BACKUP_DIR}/backup.log"
mkdir -p "${BACKUP_DIR}"
echo "[$(date -Is)] Starting backup: ${DB_HOST}:${DB_PORT}/${DB_NAME} -> ${BACKUP_FILE}" | tee -a "${LOG_FILE}"
PGPASSWORD="${DB_PASSWORD}" pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" --no-owner --no-acl | gzip > "${BACKUP_FILE}"
echo "[$(date -Is)] Backup OK: ${BACKUP_FILE} ($(du -h ${BACKUP_FILE} | cut -f1))" | tee -a "${LOG_FILE}"
find "${BACKUP_DIR}" -name "roma_*.sql.gz" -type f -mtime +${RETENTION_DAYS} -delete
echo "[$(date -Is)] Cleanup old >${RETENTION_DAYS}d done" | tee -a "${LOG_FILE}"
