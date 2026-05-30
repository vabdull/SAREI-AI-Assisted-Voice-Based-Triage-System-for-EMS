#!/usr/bin/env bash
# Lightweight smoke import — verifies the route modules still
# import without diff'd code crashing at import-time. Cheap to run
# from PowerShell where quoting a one-liner is painful.
cd /mnt/c/Users/user/Desktop/SAREI
~/ems-triage-venv/bin/python - <<'PY'
from backend.api.v1 import ambulance, hospital, dispatcher
print("import_ok")
PY
