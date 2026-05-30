"""Hit the portal endpoints as admin and dump key fields.

Used to confirm whether ``patient_count`` actually reaches the wire
(it is in the DB and in the Pydantic schema — but if the running
uvicorn process is stale it will still serialise the old shape).
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core.security import create_access_token  # noqa: E402
from backend.app.db.models import User, UserRole  # noqa: E402
from backend.app.db.session import SessionLocal  # noqa: E402


def main() -> int:
    db = SessionLocal()
    admin = db.query(User).filter(User.role == UserRole.admin).first()
    if admin is None:
        print("[probe] no admin user found", file=sys.stderr)
        return 1
    token = create_access_token(data={"sub": str(admin.id)})
    db.close()

    base = "http://127.0.0.1:8011"
    for path in (
        "/api/v1/hospital/incoming",
        "/api/v1/ambulance/my-cases",
        "/api/v1/cases/11",
    ):
        req = urllib.request.Request(
            f"{base}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            print(f"== {path} ERROR: {exc}")
            continue

        if isinstance(body, list):
            print(f"== {path}  ({len(body)} cases) ==")
            for c in body:
                print(
                    f"  case#{c['id']} {c['incident_number']:>14}  "
                    f"status={c['status']:<10}  "
                    f"patient_count={c.get('patient_count')!r:<6}  "
                    f"chief_complaint={c.get('chief_complaint')!r}"
                )
        else:
            print(f"== {path} ==")
            for k in (
                "id",
                "incident_number",
                "status",
                "patient_count",
                "chief_complaint",
                "patient_name",
                "patient_age",
            ):
                print(f"  {k}: {body.get(k)!r}")
            print(
                "  [has patient_count key?]",
                "patient_count" in body,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
