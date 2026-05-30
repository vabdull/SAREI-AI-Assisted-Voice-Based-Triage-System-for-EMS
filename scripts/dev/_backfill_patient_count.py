"""Backfill ``cases.patient_count`` for existing rows.

Iterates over every case where ``patient_count`` is NULL, joins the
case's transcript segments into a single transcript text, runs the
(now expanded) ``infer_patient_count`` regex over it, and writes the
extracted value back to the row.

Reason: the previous regex dictionary missed the bare cardinal form
``ثلاث`` (used naturally in Najdi/Hijazi speech), so every case
created before the fix has ``patient_count = NULL`` even when the
transcript clearly says "ثلاث مصابين" / "اربع مصابين" etc.

Idempotent: cases that already have a non-null value are skipped.
Pass --dry-run to preview without writing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import Case, TranscriptSegment  # noqa: E402
from backend.db.session import SessionLocal  # noqa: E402
from backend.services.fast_decision_service import (  # noqa: E402
    infer_patient_count,
)


def _stitch_transcript(segs: list[TranscriptSegment]) -> str:
    return "\n".join(s.text for s in sorted(segs, key=lambda s: s.timestamp))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="don't write")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        candidates = (
            db.query(Case).filter(Case.patient_count.is_(None)).all()
        )
        print(f"[backfill] {len(candidates)} case(s) have patient_count=NULL")

        updated = 0
        for case in candidates:
            segs = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.case_id == case.id)
                .all()
            )
            transcript = _stitch_transcript(segs)
            count = infer_patient_count(transcript)
            if count is None:
                continue

            print(
                f"  case#{case.id} {case.incident_number}: "
                f"transcript len={len(transcript)} -> patient_count={count}"
            )
            if not args.dry_run:
                case.patient_count = count
                updated += 1

        if args.dry_run:
            print("[backfill] dry-run — nothing written.")
        else:
            if updated:
                db.commit()
            print(f"[backfill] updated {updated} case(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
