"""Central logging configuration.

Installs a stream handler that flushes immediately (so logs appear in
real time under uvicorn), an optional file sink, and makes uvicorn's
own loggers propagate into our handlers for a single unified log.
"""

from __future__ import annotations

import logging
import os
import sys


class _FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every record.

    Python's default logging buffers fully when stderr is redirected to a
    file (e.g. when the backend runs under `nohup ... >>log 2>&1`). That
    makes live debugging painful because request logs never land in the
    file until the process exits. Flushing on every emit keeps the file
    up-to-date in real time.
    """

    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        super().emit(record)
        try:
            self.flush()
        except Exception:
            # swallow — logging must never raise
            pass


def configure_logging() -> None:
    """Configure root logging; called once at application startup."""
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Replace any existing handlers (e.g. basicConfig-installed ones) with
    # our flushing variant so log lines appear immediately.
    for h in list(root.handlers):
        root.removeHandler(h)
    stderr_handler = _FlushingStreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(stderr_handler)

    # Optional extra sink so even when stderr is redirected with buffering
    # we still get a reliable tail via the file path.
    extra_path = os.environ.get("EMS_LOG_FILE")
    if extra_path:
        try:
            file_handler = logging.FileHandler(extra_path, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(fmt))
            file_handler.setLevel(logging.INFO)
            root.addHandler(file_handler)
        except Exception:
            pass

    # Make uvicorn's own loggers propagate through our root handler so
    # access + error logs land in the same place.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(logging.INFO)
