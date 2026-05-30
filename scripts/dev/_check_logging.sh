#!/usr/bin/env bash
set -e
cd /mnt/c/Users/user/Desktop/SAREI
source ~/ems-hybrid-legacy/bin/activate
export PYTHONPATH=/mnt/c/Users/user/Desktop/SAREI
export EMS_LOG_FILE=/tmp/_ems_log_test.log
: > /tmp/_ems_log_test.log
python - <<'PY'
import logging
from backend.core.logging import configure_logging

configure_logging()
root = logging.getLogger()
print("root handlers:", root.handlers)
access = logging.getLogger("backend.access")
access.info("hello from test, should appear in file")
logging.getLogger("uvicorn.access").info("fake access entry")
PY
echo "--- file contents ---"
cat /tmp/_ems_log_test.log
