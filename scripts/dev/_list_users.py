import sqlite3
c = sqlite3.connect("ems_triage.db")
for r in c.execute("SELECT id, email, role FROM users ORDER BY id LIMIT 20"):
    print(r)
