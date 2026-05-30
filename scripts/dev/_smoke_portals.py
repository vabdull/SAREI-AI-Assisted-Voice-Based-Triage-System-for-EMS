"""Quick smoke test: hit each portal endpoint as the appropriate user."""

import json
import urllib.error
import urllib.request

from backend.app.core.security import create_access_token
from backend.app.db.models import User, UserRole
from backend.app.db.session import SessionLocal


def hit(uid: int, path: str) -> dict | list | str:
    tok = create_access_token(data={"sub": str(uid)})
    req = urllib.request.Request(
        f"http://127.0.0.1:8011{path}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"


db = SessionLocal()
medic = db.query(User).filter(User.role == UserRole.medic).first()
hosp = db.query(User).filter(User.role == UserRole.hospital).first()
admin = db.query(User).filter(User.role == UserRole.admin).first()
db.close()

print(f"medic:    id={medic.id} username={medic.username}")
print(f"hospital: id={hosp.id} username={hosp.username}")
print(f"admin:    id={admin.id} username={admin.username}")
print()

cases = {
    "medic /ambulance/my-cases":   hit(medic.id, "/api/v1/ambulance/my-cases"),
    "hosp  /hospital/incoming":    hit(hosp.id, "/api/v1/hospital/incoming"),
    "admin /ambulance/my-cases":   hit(admin.id, "/api/v1/ambulance/my-cases"),
    "admin /hospital/incoming":    hit(admin.id, "/api/v1/hospital/incoming"),
    "admin /cases/":               hit(admin.id, "/api/v1/cases/"),
}

for label, data in cases.items():
    if isinstance(data, str):
        print(f"{label}: {data}")
        continue
    print(f"{label}: {len(data)} rows")
    for c in data[:5]:
        med_id = c["assigned_medic"]["id"] if c.get("assigned_medic") else None
        h_id = c["assigned_hospital"]["id"] if c.get("assigned_hospital") else None
        print(
            f"  case={c['id']} status={c['status']} "
            f"triage={c.get('triage_priority')} medic={med_id} hosp={h_id}"
        )
