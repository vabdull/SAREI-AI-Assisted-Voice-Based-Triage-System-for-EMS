"""Quick DB status check: enum drift + per-status row counts."""

import re
import sqlite3
import sys

from backend.app.core.config import get_settings

path = re.sub(r"^sqlite:///", "", get_settings().database_url)
con = sqlite3.connect(path)
cur = con.cursor()

valid = {"active", "en_route", "at_scene", "transporting", "at_hospital", "closed"}

print("Canonical CaseStatus enum:", sorted(valid))
print()
print("Distinct statuses in DB:")
for row in cur.execute("SELECT status, COUNT(*) FROM cases GROUP BY status"):
    marker = "" if row[0] in valid else "  <-- INVALID"
    print(f"  {row[0]!r}: {row[1]}{marker}")

print()
print("Rows with invalid status:")
placeholders = ",".join("?" for _ in valid)
for row in cur.execute(
    f"SELECT id, incident_number, status, assigned_medic_id, assigned_hospital_id "
    f"FROM cases WHERE status NOT IN ({placeholders}) ORDER BY id DESC",
    tuple(valid),
):
    print(f"  id={row[0]} inc={row[1]} status={row[2]!r} medic={row[3]} hosp={row[4]}")

con.close()
