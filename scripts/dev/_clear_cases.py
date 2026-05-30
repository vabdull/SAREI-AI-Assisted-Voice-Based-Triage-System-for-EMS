"""Destructive: wipe every case from the database.

What this clears:
  * ``cases``                — all case rows
  * ``transcript_segments``  — all dispatcher transcript lines
  * ``call_recordings``      — DB rows AND the audio files on disk

What this preserves:
  * ``users``       — accounts stay so portals keep working
  * ``audit_logs``  — historical audit trail is never destroyed
  * users' assignments, etc. (everything not case-shaped)

Usage:
  python scripts/_clear_cases.py            # interactive confirm
  python scripts/_clear_cases.py --yes      # skip prompt

Note: the backend keeps in-memory ``CaseLiveState`` for active calls.
The DB wipe alone won't drop those — restart uvicorn afterwards to
guarantee a clean slate across both the DB and the WebSocket cache.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a standalone script from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import (  # noqa: E402
    Case,
    CallRecording,
    TranscriptSegment,
)
from backend.db.session import SessionLocal  # noqa: E402


def _count(db, model) -> int:
    return db.query(model).count()


def _delete_recording_files(paths: list[str]) -> int:
    """Best-effort: remove the recorded audio blobs from disk so we
    don't leave orphan files that clutter the storage dir. Missing
    files are silently ignored; this is a cleanup helper, not a
    correctness guarantee."""
    removed = 0
    for raw in paths:
        if not raw:
            continue
        candidates = [Path(raw), ROOT / raw]
        for p in candidates:
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
                    break
            except OSError:
                pass
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="skip confirm")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        case_n = _count(db, Case)
        seg_n = _count(db, TranscriptSegment)
        rec_n = _count(db, CallRecording)

        print(f"[clear_cases] current counts:")
        print(f"  cases               = {case_n}")
        print(f"  transcript_segments = {seg_n}")
        print(f"  call_recordings     = {rec_n}")
        if case_n == seg_n == rec_n == 0:
            print("[clear_cases] nothing to delete.")
            return 0

        if not args.yes:
            ans = input("Delete all of the above? type 'YES' to confirm: ")
            if ans.strip() != "YES":
                print("[clear_cases] aborted.")
                return 1

        rec_paths = [r.storage_path for r in db.query(CallRecording).all()]

        # Delete in FK-safe order: leaves first.
        db.query(TranscriptSegment).delete(synchronize_session=False)
        db.query(CallRecording).delete(synchronize_session=False)
        db.query(Case).delete(synchronize_session=False)
        db.commit()

        files_removed = _delete_recording_files(rec_paths)
        print(
            f"[clear_cases] done. removed {case_n} cases, {seg_n} segments, "
            f"{rec_n} recording rows, {files_removed}/{len(rec_paths)} blobs."
        )
        print(
            "[clear_cases] note: restart the backend (uvicorn) to flush "
            "any in-memory CaseLiveState left over from active calls."
        )

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
