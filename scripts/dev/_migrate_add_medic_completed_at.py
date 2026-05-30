"""One-shot migration: add ``cases.medic_completed_at`` column.

SQLite's ``ALTER TABLE`` only supports a tiny subset of SQL; this
script issues exactly the ``ADD COLUMN`` we need and is idempotent
(skips silently if the column already exists). Safe to run multiple
times.

Why we need this migration:
    The ``Case`` model gained ``medic_completed_at`` so the ambulance
    side can mark "my part is done" without closing the case for the
    hospital. ``Base.metadata.create_all`` only creates missing
    tables — it does not add missing columns to existing tables —
    so existing dev DBs need this one-shot patch.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from backend.db.session import SessionLocal, engine  # noqa: E402


COLUMN = "medic_completed_at"
SQL = f"ALTER TABLE cases ADD COLUMN {COLUMN} DATETIME"


def main() -> int:
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("cases")}
    if COLUMN in cols:
        print(f"[migrate] cases.{COLUMN} already exists — nothing to do.")
        return 0

    # ALTER must run on a committed connection on SQLite.
    with engine.begin() as conn:
        conn.execute(text(SQL))
    print(f"[migrate] added cases.{COLUMN} (NULL on every existing row).")

    # Sanity re-check.
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("cases")}
    assert COLUMN in cols, "ALTER TABLE silently failed"
    return 0


if __name__ == "__main__":
    sys.exit(main())
