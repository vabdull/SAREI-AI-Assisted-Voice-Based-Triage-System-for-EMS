#!/usr/bin/env bash
# Quick sanity check: ensure CaseRead emits UTC datetimes with a `Z`
# suffix so the browser doesn't shift them into local time.
cd /mnt/c/Users/user/Desktop/SAREI
~/ems-triage-venv/bin/python - <<'PY'
from datetime import datetime
from backend.schemas.cases import CaseRead

raw = {
    "id": 1,
    "incident_number": "T1",
    "status": "active",
    "triage_priority": None,
    "patient_name": None,
    "patient_age": None,
    "patient_gender": None,
    "chief_complaint": None,
    "patient_location": None,
    "notes": None,
    "dispatcher": None,
    "assigned_medic": None,
    "assigned_hospital": None,
    "transcript_segments": [],
    "recordings": [],
    "medic_completed_at": None,
    "created_at": datetime(2026, 5, 25, 1, 7, 0),   # naive UTC instant
    "updated_at": datetime(2026, 5, 25, 1, 7, 0),
}
case = CaseRead.model_validate(raw)
payload = case.model_dump(mode="json")
print("created_at =>", repr(payload["created_at"]))
print("updated_at =>", repr(payload["updated_at"]))
print("medic_completed_at =>", repr(payload["medic_completed_at"]))
assert payload["created_at"].endswith("Z"), "missing Z suffix"
assert payload["medic_completed_at"] is None
print("OK: all datetimes carry an explicit UTC marker.")
PY
