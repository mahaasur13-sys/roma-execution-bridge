#!/usr/bin/env bash
# Restore drill: newest offsite pg_dump → throwaway roma_drill_<ts> DB → compare
# with live. Live data is only read; the throwaway DB is dropped on every exit.
# Credentials come from a 600 root file via env only — never argv, never logs.
set -euo pipefail

CREDS=${B2_CREDS:-/root/.sealed/b2-pgdump.env}
OUT_DIR=${PGDUMP_DIR:-/root/.sealed/pgdump}
OFFSITE_STATE="$OUT_DIR/offsite_state.json"
STATE=${ROMA_DR_DRILL_STATE_FILE:-$OUT_DIR/drill_state.json}
BUCKET=${B2_BUCKET:-astrofin-mx13}
ENDPOINT=${B2_ENDPOINT:-https://s3.us-east-005.backblazeb2.com}
REGION=${B2_REGION:-us-east-005}
RCLONE=${RCLONE:-/usr/local/bin/rclone}
LIVE_DB=${LIVE_DB:-roma}

TS=$(date -u +%Y%m%dT%H%M%SZ)
DRILL_DB=${DRILL_DB:-roma_drill_$TS}
SNAP_SQL="select (select count(*) from information_schema.tables where table_schema='public'),
                 (select count(*) from execution_jobs),
                 (select count(*) from ledger_entries),
                 (select coalesce(sum(amount),0)::text from ledger_entries),
                 (select coalesce((select filename from schema_migrations
                                   order by nullif(substring(filename from '^[0-9]+'), '')::bigint desc
                                                  nulls last, filename desc limit 1),'')),
                 (select count(*) from schema_migrations)"

TMP=""
START=$(date +%s)
DUMP_AGE=0
REASON=""

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Guard: this script drops a database. Refuse anything that is not a drill DB.
case "$DRILL_DB" in
  roma_drill_*)
    if [ "$DRILL_DB" = "roma_drill_" ]; then
      log "REFUSING: drill db name is empty after prefix"
      exit 2
    fi
    ;;
  *)
    log "REFUSING: drill db name must start with roma_drill_ (got '$DRILL_DB')"
    exit 2
    ;;
esac
if [ "$DRILL_DB" = "$LIVE_DB" ]; then
  log "REFUSING: drill db equals live db"
  exit 2
fi

write_state() {
  python3 - "$STATE" "$1" "$2" "$3" "$4" <<'PY'
import json, os, sys, time

path, result, rto, age, reason = sys.argv[1:6]
try:
    with open(path) as fh:
        prev = json.load(fh)
except Exception:
    prev = {}
now = int(time.time())
state = {
    "last_run_ts": now,
    "last_success_ts": now if result == "ok" else prev.get("last_success_ts", 0),
    "result": result,
    "rto_seconds": int(rto),
    "dump_age_seconds": int(age),
    "reason": reason,
}
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(state, fh)
    fh.flush()
    os.fsync(fh.fileno())
os.replace(tmp, path)
PY
  chmod 600 "$STATE" 2>/dev/null || true
}

cleanup() {
  if [ -n "$TMP" ] && [ -d "$TMP" ]; then
    rm -rf "$TMP"
  fi
  if su postgres -c "dropdb --if-exists $DRILL_DB" >/dev/null 2>&1; then
    log "cleanup: $DRILL_DB dropped"
  else
    log "cleanup: $DRILL_DB not dropped — check manually"
  fi
}

finish() {
  rc=$?
  cleanup
  rto=$(( $(date +%s) - START ))
  if [ "$rc" -eq 0 ]; then
    write_state ok "$rto" "$DUMP_AGE" ""
  else
    write_state fail "$rto" "$DUMP_AGE" "${REASON:-exit $rc}"
  fi
  exit "$rc"
}
trap finish EXIT

snapshot() {
  printf '%s\n' "$SNAP_SQL" | su postgres -c "psql -At -F'|' -d $1"
}

ge() { python3 -c 'import sys; sys.exit(0 if float(sys.argv[2]) >= float(sys.argv[1]) else 1)' "$1" "$2"; }

if [ ! -r "$OFFSITE_STATE" ]; then
  REASON="offsite state missing: $OFFSITE_STATE"
  log "$REASON"
  exit 1
fi
object=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("object") or "")' "$OFFSITE_STATE")
if [ -z "$object" ]; then
  REASON="offsite state has no object"
  log "$REASON"
  exit 1
fi

local_dump="$OUT_DIR/$(basename "$object")"
if [ ! -r "$local_dump" ]; then
  REASON="local dump missing: $local_dump"
  log "$REASON"
  exit 1
fi
DUMP_AGE=$(( $(date +%s) - $(stat -c %Y "$local_dump") ))
local_sha=$(sha256sum "$local_dump" | cut -d' ' -f1)
log "start object=$object local=$(basename "$local_dump") sha256=$local_sha dump_age_s=$DUMP_AGE db=$DRILL_DB"

if [ ! -r "$CREDS" ]; then
  REASON="creds file not readable: $CREDS"
  log "$REASON"
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "$CREDS"
set +a
export RCLONE_CONFIG_B2_TYPE=s3 \
  RCLONE_CONFIG_B2_PROVIDER=Other \
  RCLONE_CONFIG_B2_ENDPOINT="$ENDPOINT" \
  RCLONE_CONFIG_B2_REGION="$REGION" \
  RCLONE_CONFIG_B2_ACCESS_KEY_ID="$B2_KEY_ID" \
  RCLONE_CONFIG_B2_SECRET_ACCESS_KEY="$B2_APP_KEY" \
  RCLONE_CONFIG_B2_NO_CHECK_BUCKET=true

TMP=$(mktemp -d)
if ! "$RCLONE" copyto --checksum --stats 0 --log-level ERROR "b2:$BUCKET/$object" "$TMP/dump"; then
  REASON="download failed: b2:$BUCKET/$object"
  log "$REASON"
  exit 1
fi
remote_sha=$(sha256sum "$TMP/dump" | cut -d' ' -f1)
if [ "$remote_sha" != "$local_sha" ]; then
  REASON="sha256 mismatch remote=$remote_sha local=$local_sha"
  log "$REASON"
  exit 1
fi
log "download ok sha256=$remote_sha size=$(stat -c %s "$TMP/dump")"

if ! su postgres -c "createdb $DRILL_DB"; then
  REASON="createdb failed: $DRILL_DB"
  log "$REASON"
  exit 1
fi
if ! su postgres -c "pg_restore --no-owner --no-acl -d $DRILL_DB" < "$TMP/dump"; then
  REASON="pg_restore failed: $DRILL_DB"
  log "$REASON"
  exit 1
fi
log "restore ok db=$DRILL_DB"

live=$(snapshot "$LIVE_DB")
drill=$(snapshot "$DRILL_DB")
IFS='|' read -r l_tab l_jobs l_cnt l_sum l_mig l_mign <<< "$live"
IFS='|' read -r d_tab d_jobs d_cnt d_sum d_mig d_mign <<< "$drill"
log "live tables=$l_tab jobs=$l_jobs ledger=$l_cnt/$l_sum top=$l_mig migrations=$l_mign"
log "drill tables=$d_tab jobs=$d_jobs ledger=$d_cnt/$d_sum top=$d_mig migrations=$d_mign"

fail_reason=""
ge "$d_tab" "$l_tab" || fail_reason="tables: drill $d_tab > live $l_tab"
if [ -z "$fail_reason" ]; then
  ge "$d_jobs" "$l_jobs" || fail_reason="execution_jobs: drill $d_jobs > live $l_jobs"
fi
if [ -z "$fail_reason" ]; then
  ge "$d_cnt" "$l_cnt" || fail_reason="ledger_entries count: drill $d_cnt > live $l_cnt"
fi
if [ -z "$fail_reason" ]; then
  ge "$d_sum" "$l_sum" || fail_reason="ledger_entries sum: drill $d_sum > live $l_sum"
fi
if [ -z "$fail_reason" ]; then
  ge "$d_mign" "$l_mign" || fail_reason="schema_migrations: drill $d_mign > live $l_mign"
fi
if [ -z "$fail_reason" ] && [ "$d_mig" != "$l_mig" ]; then
  if ! printf '%s\n' "select 1 from schema_migrations where filename = '$d_mig'" \
      | su postgres -c "psql -At -d $LIVE_DB" | grep -qx 1; then
    fail_reason="migration $d_mig exists only in drill"
  fi
fi
if [ -n "$fail_reason" ]; then
  REASON="$fail_reason"
  log "result=fail reason=$fail_reason"
  exit 1
fi

log "result=ok rto_s=$(( $(date +%s) - START )) dump_age_s=$DUMP_AGE"
