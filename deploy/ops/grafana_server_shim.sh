#!/usr/bin/env bash
# A4 fail-closed guard (2026-09-21): Grafana must NOT start without the sealed admin password.
# Reproduced fail-open: missing sealed file → empty GF_SECURITY_ADMIN_PASSWORD → fresh Grafana
# creates admin/admin (probe on :3001 with fresh data dir, 2026-09-21 05:34Z).
# Shim is used by the service entrypoint via PATH (/usr/local/bin precedes /usr/sbin).
set -u
F=/home/workspace/.sealed/grafana-admin.txt
if [ ! -s "$F" ]; then
  echo "FATAL(A4 fail-closed): sealed Grafana admin password missing or empty: $F - refusing to start (no default credentials)" >&2
  exit 78
fi
exec /usr/sbin/grafana-server "$@"
