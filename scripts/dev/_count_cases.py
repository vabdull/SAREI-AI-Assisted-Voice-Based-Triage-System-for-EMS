"""Quick read-only counter for case-related tables. Companion to
_clear_cases.py — use to verify before/after.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db.models import (  # noqa: E402
    Case,
    CallRecording,
    TranscriptSegment,
)
from backend.app.db.session import SessionLocal  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        print("cases:               ", db.query(Case).count())
        print("transcript_segments: ", db.query(TranscriptSegment).count())
        print("call_recordings:     ", db.query(CallRecording).count())
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
