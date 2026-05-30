"""Diagnostic snapshot: every case and every potential portal viewer.

Prints a per-case table showing which medic / hospital the case is
assigned to, and a per-user table showing what each user would see in
their portal endpoint right now. Useful when the dispatcher confirms
a case but the ambulance/hospital portal still looks empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.v1.ambulance import my_cases as ambulance_my_cases  # noqa: E402
from backend.api.v1.hospital import incoming_cases  # noqa: E402
from backend.db.models import Case, User, UserRole  # noqa: E402
from backend.db.session import SessionLocal  # noqa: E402


def main() -> int:
    db = SessionLocal()
    try:
        cases = db.query(Case).order_by(Case.id).all()
        users = db.query(User).order_by(User.id).all()

        print(f"=== {len(cases)} cases ===")
        if not cases:
            print("  (no cases — nothing for ambulance/hospital to see)")
        for c in cases:
            print(
                f"  case#{c.id:<3} status={c.status.value:<13} "
                f"medic_id={c.assigned_medic_id or '-':<5} "
                f"hospital_id={c.assigned_hospital_id or '-':<5} "
                f"dispatcher_id={c.dispatcher_id or '-'}"
            )

        print(f"\n=== {len(users)} users ===")
        for u in users:
            tag = "ACTIVE" if u.is_active else "INACTIVE"
            print(
                f"  user#{u.id:<3} role={u.role.value:<12} "
                f"username={u.username!r:<25} {tag}"
            )

        print("\n=== what each ambulance/hospital user would see ===")
        for u in users:
            if u.role == UserRole.medic:
                seen = ambulance_my_cases(db=db, current_user=u)
                ids = [c.id for c in seen]
                print(
                    f"  AMBULANCE  user#{u.id} ({u.username}): "
                    f"{len(ids)} case(s) -> {ids}"
                )
            elif u.role == UserRole.hospital:
                seen = incoming_cases(db=db, current_user=u)
                ids = [c.id for c in seen]
                print(
                    f"  HOSPITAL   user#{u.id} ({u.username}): "
                    f"{len(ids)} case(s) -> {ids}"
                )
            elif u.role == UserRole.admin:
                # admins see everything by design
                ambo = ambulance_my_cases(db=db, current_user=u)
                hosp = incoming_cases(db=db, current_user=u)
                print(
                    f"  ADMIN      user#{u.id} ({u.username}): "
                    f"ambulance={len(ambo)} hospital={len(hosp)}"
                )

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
