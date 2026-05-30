#!/usr/bin/env bash
# Restart the EMS backend cleanly. Idempotent: safe to run even if
# nothing is currently running on :8011. Uses ``setsid`` so the
# uvicorn child survives our parent shell exiting (which is what
# was happening when we tried to chain ``nohup ... & disown`` from
# a one-shot ``wsl bash -c`` invocation).
set -e

PORT="${PORT:-8011}"
VENV="${VENV:-$HOME/ems-venv}"
LOG="${LOG:-/tmp/ems_backend.log}"
PROJECT="${PROJECT:-/mnt/c/Users/user/Desktop/SAREI}"

echo "[restart_backend] stopping any existing uvicorn on :$PORT"
pkill -f "uvicorn backend.main" 2>/dev/null || true
sleep 2

cd "$PROJECT"
echo "[restart_backend] spawning uvicorn (venv=$VENV port=$PORT log=$LOG)"
setsid nohup "$VENV/bin/uvicorn" backend.main:app \
    --host 127.0.0.1 --port "$PORT" \
    --log-level info --no-use-colors \
    </dev/null >"$LOG" 2>&1 &
disown || true

# Give uvicorn ~2s to register its child pid before we exit so the
# caller's `ps` sees something. The actual readiness check is up to
# _wait_backend.sh.
sleep 2
echo "[restart_backend] spawned. tail -f $LOG to watch."
