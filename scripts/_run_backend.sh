#!/usr/bin/env bash
# Runs the EMS backend with unbuffered logs going directly to /tmp/ems-backend.log.
# No tee / pipeline so uvicorn's buffering behaviour is simple and deterministic.
#
# Port 8011 is the canonical dev port: the Vite proxy in frontend/vite.config.ts
# forwards /api to 127.0.0.1:8011 and the WSL ems-venv has the NeMo ASR stack.
set -e
fuser -k 8011/tcp 2>/dev/null || true
sleep 1
cd /mnt/c/Users/user/Desktop/SAREI
source ~/ems-venv/bin/activate
export PYTHONPATH=/mnt/c/Users/user/Desktop/SAREI
export PYTHONUNBUFFERED=1
LOG=/tmp/ems-backend.log
export EMS_LOG_FILE="$LOG"
: > "$LOG"
exec uvicorn backend.main:app \
  --host 127.0.0.1 --port 8011 \
  --log-level info --no-use-colors \
  >>"$LOG" 2>&1
