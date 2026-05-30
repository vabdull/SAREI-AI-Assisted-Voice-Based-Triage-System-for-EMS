#!/usr/bin/env bash
set -e
cd /mnt/c/Users/user/Desktop/SAREI
source ~/ems-hybrid-legacy/bin/activate
export PYTHONPATH=/mnt/c/Users/user/Desktop/SAREI
python - <<'PY'
import traceback
try:
    from backend.app.api.v1 import inference
    print("inference module imported OK")
except Exception:
    traceback.print_exc()
    raise
try:
    from backend.app.triage_engine import get_triage_engine
    eng = get_triage_engine()
    print("triage engine:", type(eng).__name__)
except Exception:
    traceback.print_exc()
    raise
PY
