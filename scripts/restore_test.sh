#!/usr/bin/env bash
# Verify the newest Redis backup can actually be restored: checksum, load into a scratch
# Redis instance, and compare the key count recorded at backup time.
# Usage: scripts/restore_test.sh [--dir backups] [--port 6391]
set -uo pipefail

DIR=backups; PORT=6391
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)  DIR="$2";  shift 2;;
    --port) PORT="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

LATEST=$(ls -1t "$DIR"/redis-*.rdb 2>/dev/null | head -n 1)
[[ -n "$LATEST" ]] || { echo "FAIL: no backups found in $DIR" >&2; exit 1; }

( cd "$DIR" && sha256sum -c "$(basename "$LATEST").sha256" >/dev/null 2>&1 ) \
  || { echo "FAIL: checksum mismatch for $LATEST" >&2; exit 1; }

EXPECTED=$(sed -n 's/^keys=//p' "$LATEST.meta")
WORK=$(mktemp -d)
cp "$LATEST" "$WORK/dump.rdb"
redis-server --port "$PORT" --dir "$WORK" --dbfilename dump.rdb --save "" --appendonly no \
  --daemonize yes --pidfile "$WORK/redis.pid" --logfile "$WORK/redis.log" >/dev/null

for _ in $(seq 1 50); do
  redis-cli -p "$PORT" ping >/dev/null 2>&1 && break
  sleep 0.1
done

ACTUAL=$(redis-cli -p "$PORT" dbsize 2>/dev/null | awk '{print $NF}')
redis-cli -p "$PORT" shutdown nosave >/dev/null 2>&1 || true
rm -rf "$WORK"

if [[ "$ACTUAL" == "$EXPECTED" ]]; then
  echo "PASS: restored $LATEST, keys expected=$EXPECTED actual=$ACTUAL"
else
  echo "FAIL: key count mismatch for $LATEST, expected=$EXPECTED actual=${ACTUAL:-none}" >&2
  exit 1
fi
