#!/bin/bash
# Daily PostgreSQL backup daemon — sleeps until 02:00 UTC then runs backup.sh
BACKUP_SCRIPT="/home/workspace/roma-execution-bridge/backup.sh"

while true; do
    # Calculate seconds until next 02:00 UTC
    CURRENT_H=$(date -u +%H)
    CURRENT_M=$(date -u +%M)
    CURRENT_S=$(date -u +%S)
    
    if [ "$CURRENT_H" -lt 2 ]; then
        TARGET_H=2
    else
        TARGET_H=26  # next day
    fi
    
    SLEEP_SEC=$(( (TARGET_H - CURRENT_H) * 3600 - CURRENT_M * 60 - CURRENT_S ))
    [ $SLEEP_SEC -lt 0 ] && SLEEP_SEC=86400  # safety
    
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') Sleeping ${SLEEP_SEC}s until next 02:00 UTC"
    sleep $SLEEP_SEC
    
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') Running backup..."
    $BACKUP_SCRIPT 2>&1
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') Backup cycle complete"
done
