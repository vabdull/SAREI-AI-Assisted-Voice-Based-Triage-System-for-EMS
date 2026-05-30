#!/usr/bin/env bash
# Block until the backend reports healthy on 127.0.0.1:8011, or
# timeout. Avoids the PowerShell quoting nightmare of inlining a
# busy-poll loop into a single `wsl bash -lc "..."` call.
set -e

MAX_WAIT="${MAX_WAIT:-60}"
PORT="${PORT:-8011}"

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT" ]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/health" || true)
    if [ "$code" = "200" ]; then
        echo "[wait_backend] ready after ${elapsed}s"
        exit 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
    echo "[wait_backend] waiting... ${elapsed}s (code=$code)"
done

echo "[wait_backend] timed out after ${MAX_WAIT}s"
exit 1
