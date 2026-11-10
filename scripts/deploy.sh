#!/usr/bin/env bash
# Deploy a release with a post-deploy health check and automatic rollback.
# Mirrors a CI/CD "deploy -> verify -> rollback on failure" step on a single host.
#
# Usage: scripts/deploy.sh <release-tag> [--break-ready]
#   --break-ready  simulates a bad release whose /ready endpoint fails (for drills)
# Env: RUNTIME_DIR (default ./runtime), PORT (8099), REDIS_URL, HEALTH_TRIES (20), HEALTH_SLEEP (0.25)
#
# Exit codes: 0 deployed, 1 deploy failed and previous release restored, 3 failed with no previous release.
set -uo pipefail

TAG="${1:?usage: deploy.sh <release-tag> [--break-ready]}"
BREAK=0; [[ "${2:-}" == "--break-ready" ]] && BREAK=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="${RUNTIME_DIR:-$ROOT/runtime}"
PORT="${PORT:-8099}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TRIES="${HEALTH_TRIES:-20}"; SLEEP="${HEALTH_SLEEP:-0.25}"
REL="$RUNTIME/releases"; PIDFILE="$RUNTIME/app.pid"; CURRENT="$RUNTIME/current"; LOG="$RUNTIME/logs"
mkdir -p "$REL" "$LOG"

log() { echo "[$(date -u +%H:%M:%S.%3N)] $*"; }

stop_app() {
  if [[ -f "$PIDFILE" ]]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    for _ in $(seq 1 40); do kill -0 "$(cat "$PIDFILE")" 2>/dev/null || break; sleep 0.1; done
    kill -9 "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
}

start_release() {  # $1=tag $2=break(0/1)
  local dir="$REL/$1"
  # Only the server command is backgrounded, with all of its stdio detached; otherwise a caller
  # capturing this script's output (CI step, drill runner) would block until the server exits.
  ( cd "$dir"
    APP_VERSION="$1" BREAK_READY="$2" REDIS_URL="$REDIS_URL" ENABLE_FAULT_INJECTION="${ENABLE_FAULT_INJECTION:-0}" DRILLS_RESULTS_PATH="$ROOT/drills/results/results.json" \
    nohup python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level warning \
    >>"$LOG/app-$1.log" 2>&1 </dev/null &
    echo $! >"$PIDFILE" )
}

wait_ready() {
  for _ in $(seq 1 "$TRIES"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:$PORT/ready" || true)
    [[ "$code" == "200" ]] && return 0
    sleep "$SLEEP"
  done
  return 1
}

PREV=""; [[ -L "$CURRENT" ]] && PREV="$(basename "$(readlink "$CURRENT")")"

log "deploying release $TAG (previous: ${PREV:-none})"
rm -rf "$REL/$TAG" && mkdir -p "$REL/$TAG" && cp -r "$ROOT/app" "$REL/$TAG/app"
stop_app
start_release "$TAG" "$BREAK"

if wait_ready; then
  ln -sfn "$REL/$TAG" "$CURRENT"
  log "release $TAG healthy; deploy complete"
  exit 0
fi

log "release $TAG FAILED health check; rolling back"
stop_app
if [[ -n "$PREV" && -d "$REL/$PREV" ]]; then
  start_release "$PREV" 0
  if wait_ready; then
    log "rollback to $PREV complete and healthy"
    exit 1
  fi
fi
log "rollback failed or no previous release available"
exit 3
