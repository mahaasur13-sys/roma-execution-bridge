#!/usr/bin/env bash
# Weekly restore drill: Sundays 04:30Z, one hour after the 03:30Z pg_dump that
# AstroFin's pgdump_daily.sh produces. Long-lived loop — supervisord keeps it
# alive (this host has no cron), same pattern as astrofin-pgdump.
#
# No credentials here: pg_restore_drill.sh reads its own 600 root env file
# (B2_CREDS, default /root/.sealed/b2-pgdump.env) and never logs values.
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DRILL=${DRILL_SCRIPT:-$SCRIPT_DIR/pg_restore_drill.sh}
LOCK=${DRILL_LOCK:-/root/.sealed/pgdump/drill.lock}
TIMEOUT_S=${DRILL_TIMEOUT_S:-1800}
RETRY_S=${DRILL_RETRY_S:-1800}
MAX_ATTEMPTS=${DRILL_MAX_ATTEMPTS:-3}
TARGET_HHMM=${DRILL_TARGET_HHMM:-04:30}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
nap() { sleep "$1" & wait $!; }

# Seconds until the next Sunday TARGET_HHMM UTC (dow 7 = Sunday).
next_run_sleep() {
  local now base dow target
  now=$(date -u +%s)
  base=$(date -u -d "$(date -u +%F) $TARGET_HHMM" +%s)
  dow=$(date -u -d "@$now" +%u)
  target=$(( base + ((7 - dow) % 7) * 86400 ))
  [ "$target" -le "$now" ] && target=$(( target + 7 * 86400 ))
  printf '%s' $(( target - now ))
}

# flock -n -E 75: 75 means "another drill already holds the lock", skip the week.
run_once() {
  log "drill start (timeout ${TIMEOUT_S}s)"
  flock -n -E 75 "$LOCK" \
    timeout --signal=TERM --kill-after=60 "$TIMEOUT_S" bash "$DRILL"
  local rc=$?
  if [ "$rc" -eq 75 ]; then

    log "lock busy ($LOCK) — another drill is running, skipping"
  else
    log "drill rc=$rc"
  fi
  return "$rc"
}

trap 'log "stopped (signal)"; exit 0' TERM INT

mkdir -p "$(dirname "$LOCK")"
log "weekly restore drill: Sundays $TARGET_HHMM UTC, lock=$LOCK timeout=${TIMEOUT_S}s"

s=$(next_run_sleep)
log "sleep ${s}s until first Sunday $TARGET_HHMM UTC"
nap "$s"

while :; do
  attempts=0
  while :; do
    attempts=$((attempts + 1))
    run_once
    rc=$?
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 75 ]; then
      break
    fi
    if [ "$attempts" -ge "$MAX_ATTEMPTS" ]; then
      log "giving up after $attempts attempts, waiting for next Sunday $TARGET_HHMM UTC"
      break
    fi
    log "retry in ${RETRY_S}s (attempt $attempts/$MAX_ATTEMPTS)"
    nap "$RETRY_S"
  done
  s=$(next_run_sleep)
  log "sleep ${s}s until next Sunday $TARGET_HHMM UTC"
  nap "$s"
done
