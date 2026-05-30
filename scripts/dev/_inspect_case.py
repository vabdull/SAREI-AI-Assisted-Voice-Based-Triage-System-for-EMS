"""Read-only inspector for a single case.

Pass the incident_number or id to see what the DB and live state
service both think the case looks like. Useful when the UI shows a
field as empty and we need to figure out whether the data was never
extracted, never mirrored, or never serialised correctly.

Usage:
  python scripts/_inspect_case.py C14DE143C076
  python scripts/_inspect_case.py 42
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import Case  # noqa: E402
from backend.db.session import SessionLocal  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    needle = argv[0]

    db = SessionLocal()
    try:
        q = db.query(Case)
        if needle.isdigit():
            case = q.filter(Case.id == int(needle)).first()
        else:
            case = q.filter(Case.incident_number == needle).first()
        if case is None:
            print(f"[inspect] no case with {needle!r}")
            return 1

        print(f"--- case#{case.id} {case.incident_number} ---")
        for col in (
            "status",
            "triage_priority",
            "patient_name",
            "patient_age",
            "patient_gender",
            "patient_count",
            "chief_complaint",
            "assigned_medic_id",
            "assigned_hospital_id",
            "medic_completed_at",
            "ai_confidence",
            "created_at",
            "updated_at",
        ):
            print(f"  {col:<22} = {getattr(case, col)!r}")

        print("\n--- patient_location ---")
        print(json.dumps(case.patient_location, indent=2, ensure_ascii=False))

        print("\n--- ai_triage_suggestion (truncated) ---")
        suggestion = case.ai_triage_suggestion or {}
        if isinstance(suggestion, dict):
            for k, v in suggestion.items():
                if isinstance(v, (dict, list)):
                    s = json.dumps(v, ensure_ascii=False)
                    if len(s) > 200:
                        s = s[:200] + "..."
                    print(f"  {k}: {s}")
                else:
                    print(f"  {k}: {v}")

        # In-process live state, if the backend is the same process
        # (it isn't when running this from CLI). Just note that the
        # WS state lives in the uvicorn process.
        print("\n[inspect] note: live CaseLiveState is per-process — "
              "if you need to see it, query the running backend.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
