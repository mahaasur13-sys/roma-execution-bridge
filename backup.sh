#!/bin/bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backup/postgres}"
DB_NAME="${DB_NAME:-roma}"
DB_HOST="${DB_HOST:-localhost}"
DB_USER="${DB_USER:-postgres}"
DB_PORT="${DB_PORT:-5432}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
TIMESTAMP="$(date +%Y-%m-%d_%H%M%S)"

mkdir -p "$BACKUP_DIR"

BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_${TIMESTAMP}.sql.gz"

PGPASSWORD="${DB_PASSWORD:-postgres}" pg_dump \
  -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -p "$DB_PORT" \
  --no-owner --no-acl --clean --if-exists 2>/dev/null \
  | gzip > "$BACKUP_FILE"

if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    echo "❌ pg_dump failed" >&2
    exit 1
fi

SIZE=$(stat -c%s "$BACKUP_FILE" 2>/dev/null || stat -f%z "$BACKUP_FILE" 2>/dev/null)
echo "✅ Backup: $BACKUP_FILE ($SIZE bytes)"

# Rotation
DELETED=$(find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete -print 2>/dev/null | wc -l)
echo "🧹 Rotation: removed $DELETED old backups (>${RETENTION_DAYS}d)"
echo "📦 Current: $(find "$BACKUP_DIR" -name '*.sql.gz' | wc -l) backups stored"
