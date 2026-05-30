"""Database bootstrap and lightweight SQLite migrations.

``init_database`` is called on startup to create any missing tables and
apply small additive migrations (new columns / backfills) so an existing
``ems_triage.db`` keeps working without a heavyweight migration tool.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import inspect, text

from backend.core.config import ROOT_DIR, get_settings
from backend.db.base import Base
from backend.db.models import (  # noqa: F401 – import so tables are registered
    AuditLog,
    CallRecording,
    Case,
    TranscriptSegment,
    User,
)
from backend.db.session import engine

logger = logging.getLogger(__name__)


def _migrate_legacy_location_fields() -> None:
    """One-shot migration: collapse legacy `location_text/lat/lon` columns on
    `cases` into the canonical JSON `patient_location` column.

    This is best-effort for SQLite (the live DB backend) and idempotent — if
    the legacy columns are already gone, it does nothing.
    """
    try:
        inspector = inspect(engine)
        if "cases" not in inspector.get_table_names():
            return
        columns = {col["name"] for col in inspector.get_columns("cases")}
    except Exception:
        logger.exception("Failed to inspect cases table for location migration")
        return

    has_legacy = {"location_text", "location_lat", "location_lon"} & columns
    has_new = "patient_location" in columns

    if not has_new:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE cases ADD COLUMN patient_location JSON"))
        except Exception:
            logger.exception("Failed to add patient_location column")
            return

    if not has_legacy:
        return

    # Backfill canonical patient_location JSON from the legacy columns, then
    # drop the legacy columns so there is exactly one source of truth.
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, location_text, location_lat, location_lon, patient_location "
                    "FROM cases"
                )
            ).fetchall()
            for row in rows:
                case_id, loc_text, loc_lat, loc_lon, existing = row
                if existing:
                    continue
                raw_text = (loc_text or "").strip()
                if not raw_text and loc_lat is None and loc_lon is None:
                    continue
                geocode = None
                if loc_lat is not None and loc_lon is not None:
                    geocode = {
                        "lat": float(loc_lat),
                        "lng": float(loc_lon),
                        "confidence": 0.0,
                        "provider": "legacy",
                        "match_type": None,
                    }
                payload = {
                    "raw_text": raw_text,
                    "source_span": None,
                    "components": {
                        "street": None,
                        "district": None,
                        "city": None,
                        "landmark": None,
                        "governorate": None,
                    },
                    "geocode": geocode,
                    "confidence": 0.0,
                    "needs_confirmation": True,
                }
                conn.execute(
                    text("UPDATE cases SET patient_location = :p WHERE id = :id"),
                    {"p": json.dumps(payload, ensure_ascii=False), "id": case_id},
                )

            # SQLite < 3.35 can't DROP COLUMN. Best-effort try, otherwise leave
            # the legacy columns behind (they will stay unused).
            for legacy in ("location_text", "location_lat", "location_lon"):
                try:
                    conn.execute(text(f"ALTER TABLE cases DROP COLUMN {legacy}"))
                except Exception:
                    logger.info(
                        "SQLite refused to drop legacy column %s; leaving in place",
                        legacy,
                    )
    except Exception:
        logger.exception("Failed to backfill patient_location from legacy columns")


def _migrate_add_case_columns() -> None:
    """Idempotently add newer ``cases`` columns to an existing SQLite DB.

    ``Base.metadata.create_all`` only creates *missing tables*, never
    new columns on a table that already exists. So when we add a column
    to the ``Case`` model, a previously-created DB won't have it and
    every read/write 500s. This best-effort ALTER mirrors the existing
    ``_migrate_legacy_location_fields`` pattern.
    """
    try:
        inspector = inspect(engine)
        if "cases" not in inspector.get_table_names():
            return
        columns = {col["name"] for col in inspector.get_columns("cases")}
    except Exception:
        logger.exception("Failed to inspect cases table for column migration")
        return

    # (column name, DDL type + default) — keep defaults so existing rows
    # are backfilled by SQLite at ALTER time.
    pending: list[tuple[str, str]] = []
    if "source" not in columns:
        pending.append(("source", "VARCHAR(20) NOT NULL DEFAULT 'voice'"))
    if "manual_details" not in columns:
        pending.append(("manual_details", "JSON"))

    for name, ddl in pending:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE cases ADD COLUMN {name} {ddl}"))
            logger.info("Added cases.%s column", name)
        except Exception:
            logger.exception("Failed to add cases.%s column", name)


def init_database() -> None:
    """Create tables, apply additive migrations, and ensure storage dirs."""
    Base.metadata.create_all(bind=engine)
    _migrate_legacy_location_fields()
    _migrate_add_case_columns()

    settings = get_settings()
    Path(ROOT_DIR / settings.recording_storage_dir).mkdir(parents=True, exist_ok=True)
    Path(ROOT_DIR / settings.stream_temp_dir).mkdir(parents=True, exist_ok=True)
