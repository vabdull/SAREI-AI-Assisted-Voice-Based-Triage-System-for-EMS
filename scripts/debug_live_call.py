"""Debug helper: transcribe every recording for a given case id."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from backend.ai.asr_runtime import StreamingAsrService


def _to_wsl_path(windows_path: str) -> str:
    p = windows_path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        return f"/mnt/{drive}{p[2:]}"
    return p


def main() -> None:
    case_id = int(sys.argv[1]) if len(sys.argv) > 1 else 22
    db_path = "/mnt/c/Users/user/Desktop/SAREI/ems_triage.db"
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "select id, storage_path from call_recordings where case_id=? order by id",
        (case_id,),
    ).fetchall()
    if not rows:
        print(f"No recordings for case {case_id}")
        return
    asr = StreamingAsrService()
    for rid, storage in rows:
        wsl_path = _to_wsl_path(storage)
        if not Path(wsl_path).is_file():
            print(rid, "MISSING", wsl_path)
            continue
        try:
            result = asr.transcribe_files([wsl_path])[0]
            duration = result.preprocessing.output_duration_seconds if result.preprocessing else None
            print(rid, repr(result.text), duration)
        except Exception as exc:  # pragma: no cover - debug path
            print(rid, "ERR", type(exc).__name__, str(exc)[:160])


if __name__ == "__main__":
    main()
