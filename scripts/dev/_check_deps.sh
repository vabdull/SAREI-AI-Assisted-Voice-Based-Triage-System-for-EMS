#!/usr/bin/env bash
set -e
cd /mnt/c/Users/user/Desktop/SAREI
source ~/ems-hybrid-legacy/bin/activate
export PYTHONPATH=/mnt/c/Users/user/Desktop/SAREI
python - <<'PY'
import importlib
for m in ("rapidfuzz", "pyarabic", "yaml"):
    try:
        mod = importlib.import_module(m)
        print("OK", m, getattr(mod, "__version__", "n/a"))
    except Exception as e:
        print("MISSING", m, repr(e))

print("--- triage engine import ---")
try:
    from backend.app.triage_engine import get_triage_engine
    e = get_triage_engine()
    print("engine ok", len(e.bank.symptoms), "symptoms", len(e.bank.phrase_index), "phrases")
except Exception as ex:
    import traceback; traceback.print_exc()
PY
