#!/usr/bin/env bash
# Routine maintenance: disk report, log rotation/cleanup, and a pending-updates report.
# Updates are only REPORTED by default. Pass --apply-updates to install them (needs root).
# Usage: scripts/maintenance.sh [--logs logs] [--keep-days 7] [--apply-updates]
set -uo pipefail

LOGS=logs; KEEP_DAYS=7; APPLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs) LOGS="$2"; shift 2;;
    --keep-days) KEEP_DAYS="$2"; shift 2;;
    --apply-updates) APPLY=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

echo "== disk usage =="
df -h / | tail -n 1

echo "== log cleanup in $LOGS =="
if [[ -d "$LOGS" ]]; then
  # compress logs older than 1 day, delete compressed logs older than KEEP_DAYS
  find "$LOGS" -type f \( -name '*.log' -o -name '*.jsonl' \) -mtime +1 -exec gzip -f {} \; 2>/dev/null
  REMOVED=$(find "$LOGS" -type f -name '*.gz' -mtime +"$KEEP_DAYS" -print -delete | wc -l)
  echo "removed $REMOVED compressed logs older than $KEEP_DAYS days"
else
  echo "no log directory at $LOGS"
fi

echo "== pending package updates =="
if command -v apt-get >/dev/null 2>&1; then
  COUNT=$(apt list --upgradable 2>/dev/null | grep -vc '^Listing' || true)
  echo "upgradable packages: $COUNT"
  if [[ $APPLY -eq 1 ]]; then
    apt-get update -q && apt-get -y -q upgrade
  else
    echo "dry run: re-run with --apply-updates to install"
  fi
else
  echo "apt-get not available on this host"
fi
