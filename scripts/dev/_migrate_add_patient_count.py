"""One-shot migration: add ``cases.patient_count`` column.

Idempotent — safe to run multiple times. See the sibling
``_migrate_add_medic_completed_at.py`` for the rationale on why we
hand-write ALTER TABLEs against the live SQLite DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from backend.app.db.session import engine  # noqa: E402


COLUMN = "patient_count"
SQL = f"ALTER TABLE cases ADD COLUMN {COLUMN} INTEGER"


def main() -> int:
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("cases")}
    if COLUMN in cols:
        print(f"[migrate] cases.{COLUMN} already exists — nothing to do.")
        return 0
    with engine.begin() as conn:
        conn.execute(text(SQL))
    print(f"[migrate] added cases.{COLUMN} (NULL on every existing row).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
