#!/usr/bin/env bash
# Backup Redis to a timestamped RDB file with checksum and metadata, then prune old backups.
# Usage: scripts/backup_redis.sh [--port 6379] [--host 127.0.0.1] [--dir backups] [--keep 5]
set -euo pipefail

HOST=127.0.0.1; PORT=6379; DIR=backups; KEEP=5
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --dir)  DIR="$2";  shift 2;;
    --keep) KEEP="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

mkdir -p "$DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DIR/redis-$TS.rdb"

redis-cli -h "$HOST" -p "$PORT" ping >/dev/null || { echo "ERROR: Redis not reachable at $HOST:$PORT" >&2; exit 1; }
KEYS=$(redis-cli -h "$HOST" -p "$PORT" dbsize | awk '{print $NF}')
redis-cli -h "$HOST" -p "$PORT" --rdb "$OUT" >/dev/null 2>&1
[[ -s "$OUT" ]] || { echo "ERROR: backup file empty" >&2; exit 1; }
( cd "$DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
echo "keys=$KEYS" > "$OUT.meta"
echo "backup ok: $OUT ($(stat -c %s "$OUT") bytes, $KEYS keys)"

# Retention: keep the newest $KEEP backups
ls -1t "$DIR"/redis-*.rdb 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -f "$old" "$old.sha256" "$old.meta"
  echo "pruned: $old"
done
