# Restore drill — weekly runbook

**What:** `scripts/pg_restore_drill.sh` restores the newest offsite `pg_dump` from
B2 (`astrofin-mx13/pgdump/…`) into a throwaway `roma_drill_<ts>` database on the
live PostgreSQL, compares it with the live `roma` DB, then drops it.

**When:** Sundays **04:30Z** — one hour after the 03:30Z dump, so the drill always
tests the dump of the same night.

**How it is scheduled:** `scripts/pg_restore_drill_weekly.sh` is a long-lived loop
(supervisord program `roma-dr-restore-drill`, sample
`deploy/supervisord/roma-dr-restore-drill.conf`). The loop sleeps until the next
Sunday 04:30Z, takes `flock -n` on `/root/.sealed/pgdump/drill.lock` (a second
instance skips instead of racing), runs the drill under a 30 min `timeout`, and on
failure retries after 30 min up to 3 attempts before waiting for the next Sunday.
Log: `/dev/shm/roma-dr-restore-drill.log` (supervisord rotation, 10 MB × 3).

**Guards in the drill itself:**

- `DRILL_DB` must match `^roma_drill_[A-Za-z0-9_]+$` — otherwise `exit 2`, nothing is created or dropped.
- dump age > 26 h (`MAX_DUMP_AGE_S`, default 93600) — `exit 1`, state `fail`. A staler dump would "verify" a restore that says nothing about today's backups.
- the throwaway DB is dropped only if `createdb` succeeded; tmp dir removed in `trap EXIT`.

**Result:** `/root/.sealed/pgdump/drill_state.json` (600) —
`last_run_ts`, `last_success_ts`, `result`, `rto_seconds`, `dump_age_seconds`, `reason`.
Exported by ROMA `/metrics` as `roma_dr_drill_last_success_timestamp` and
`roma_dr_drill_rto_seconds`; alert `ROMADRDrillStale` fires when the last success
is older than 8 days.

**Credentials:** only `/root/.sealed/b2-pgdump.env` (`B2_KEY_ID`, `B2_APP_KEY`,
mode 600, root), sourced by the drill — never on argv, never in logs.

## Manual run

```bash
cd /home/workspace/roma-execution-bridge
bash scripts/pg_restore_drill.sh; echo "rc=$?"
python3 -m json.tool /root/.sealed/pgdump/drill_state.json
```

## Enable on the host

```bash
cat deploy/supervisord/roma-dr-restore-drill.conf >> /etc/zo/supervisord-user.conf
supervisorctl -c /etc/zo/supervisord-user.conf update
supervisorctl -c /etc/zo/supervisord-user.conf status roma-dr-restore-drill
tail -5 /dev/shm/roma-dr-restore-drill.log
```

## Verify the first run

1. `jq .last_run_ts,.result /root/.sealed/pgdump/drill_state.json` — `result: "ok"`.
2. `curl -s localhost:8900/metrics | grep roma_dr_drill_last_success_timestamp` — non-zero.
3. `ROMADRDrillStale` stays `inactive`; `roma_drill_*` databases are gone (`psql -l`).

## Rollback

```bash
supervisorctl -c /etc/zo/supervisord-user.conf stop roma-dr-restore-drill
supervisorctl -c /etc/zo/supervisord-user.conf remove roma-dr-restore-drill
# and delete the [program:roma-dr-restore-drill] block from /etc/zo/supervisord-user.conf
```

Drill state and dumps are left untouched.
