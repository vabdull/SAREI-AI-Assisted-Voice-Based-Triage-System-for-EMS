"""Request/response models for emergency cases and their recordings.

Defines how cases are created (voice or manual), updated, and read, plus
the patient-location and call-recording sub-models. Datetime fields are
serialised as explicit UTC ISO-8601 strings via ``_iso_utc`` so the
frontend never has to guess the timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime as ISO-8601 with an explicit ``Z`` suffix.

    The DB column is a naive SQLite ``DATETIME`` written via
    ``datetime.now(timezone.utc)`` — i.e. the value is a UTC instant
    but the tzinfo is dropped on round-trip. Without an explicit
    suffix, ``new Date(...)`` in the browser interprets the string
    as **local time** and the UI displays times shifted by the
    user's UTC offset (e.g. 1 AM instead of 4 AM in Riyadh).

    Forcing ``Z`` on serialisation fixes the display everywhere
    without requiring a DB migration to a tz-aware column.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Normalise to UTC then drop microseconds for a stable, compact
    # representation. Always emit ``...Z`` instead of ``+00:00`` so
    # the wire format is unambiguous to every JSON consumer.
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )

from backend.app.db.models import CaseStatus, TriagePriority

from .auth import UserRead
from .location import PatientLocation


_VALID_CASE_STATUSES = {member.value for member in CaseStatus}
_VALID_TRIAGE_PRIORITIES = {member.value for member in TriagePriority}


class CaseCreate(BaseModel):
    patient_name: Optional[str] = None
    patient_age: Optional[int] = None
    patient_gender: Optional[str] = None
    chief_complaint: Optional[str] = None
    # Canonical structured location. Omit when unknown.
    patient_location: Optional[PatientLocation] = None
    notes: Optional[str] = None

    # ── Manual Case Entry fields (optional; ignored by the voice flow) ──
    # ``source`` defaults to "voice" at the DB layer; the manual form
    # sends "manual". Number of patients + an up-front severity may be
    # set directly by the dispatcher (the live pipeline derives these
    # automatically, so the voice flow omits them).
    source: Optional[str] = None
    patient_count: Optional[int] = None
    triage_priority: Optional[str] = None
    # Structured clinical detail (emergency_type, consciousness,
    # breathing, symptoms, ...) retained verbatim for the report.
    manual_details: Optional[dict] = None

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v):
        if v is None:
            return v
        if v not in {"voice", "manual"}:
            raise ValueError(f"Invalid source {v!r}. Allowed: ['manual', 'voice']")
        return v

    @field_validator("triage_priority")
    @classmethod
    def _validate_triage_priority(cls, v):
        if v is None:
            return v
        if v not in _VALID_TRIAGE_PRIORITIES:
            raise ValueError(
                "Invalid triage_priority "
                f"{v!r}. Allowed: {sorted(_VALID_TRIAGE_PRIORITIES)}"
            )
        return v

    @field_validator("patient_age", "patient_count")
    @classmethod
    def _validate_non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("Value must be non-negative")
        return v


class CaseUpdate(BaseModel):
    patient_name: Optional[str] = None
    patient_age: Optional[int] = None
    patient_gender: Optional[str] = None
    patient_count: Optional[int] = None
    chief_complaint: Optional[str] = None
    patient_location: Optional[PatientLocation] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    triage_priority: Optional[str] = None
    assigned_medic_id: Optional[int] = None
    assigned_hospital_id: Optional[int] = None

    @field_validator("patient_age", "patient_count")
    @classmethod
    def _validate_non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("Value must be non-negative")
        return v

    # Enum-bound fields. The DB columns use SQLAlchemy ``Enum`` types,
    # so writing an unrecognised string corrupted the row (or 500'd on
    # commit). Validating at the schema boundary turns the bad request
    # into a 422 before it ever reaches the ORM.
    @field_validator("status")
    @classmethod
    def _validate_status(cls, v):
        if v is None:
            return v
        if v not in _VALID_CASE_STATUSES:
            raise ValueError(
                f"Invalid status {v!r}. Allowed: {sorted(_VALID_CASE_STATUSES)}"
            )
        return v

    @field_validator("triage_priority")
    @classmethod
    def _validate_triage_priority(cls, v):
        if v is None:
            return v
        if v not in _VALID_TRIAGE_PRIORITIES:
            raise ValueError(
                "Invalid triage_priority "
                f"{v!r}. Allowed: {sorted(_VALID_TRIAGE_PRIORITIES)}"
            )
        return v


class TranscriptSegmentCreateRequest(BaseModel):
    speaker: str
    text: str
    is_ai_generated: bool = False
    confidence: Optional[float] = None


class TranscriptSegmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int
    speaker: str
    text: str
    timestamp: float
    is_ai_generated: bool
    confidence: Optional[float] = None
    created_at: datetime

    @field_serializer("created_at")
    def _ser_created_at(self, dt: datetime) -> str:
        return _iso_utc(dt) or ""


class CallRecordingRead(BaseModel):
    """Typed view of a call recording returned with a case.

    Giving ``CaseRead.recordings`` this explicit element type (rather than
    a bare list of ORM objects) lets Pydantic serialise recordings safely
    on every endpoint that returns a case.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int
    original_filename: str
    status: str
    duration_seconds: Optional[float] = None
    created_at: datetime

    @field_serializer("created_at")
    def _ser_created_at(self, dt: datetime) -> str:
        return _iso_utc(dt) or ""


class CaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_number: str
    status: str
    source: str = "voice"
    triage_priority: Optional[str] = None
    manual_details: Optional[dict] = None
    patient_name: Optional[str] = None
    patient_age: Optional[int] = None
    patient_gender: Optional[str] = None
    patient_count: Optional[int] = None
    chief_complaint: Optional[str] = None
    patient_location: Optional[PatientLocation] = None
    notes: Optional[str] = None
    dispatcher: Optional[UserRead] = None
    assigned_medic: Optional[UserRead] = None
    assigned_hospital: Optional[UserRead] = None
    transcript_segments: list[TranscriptSegmentRead] = Field(default_factory=list)
    recordings: list[CallRecordingRead] = Field(default_factory=list)
    medic_completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at", "medic_completed_at")
    def _ser_dt_utc(self, dt: Optional[datetime]) -> Optional[str]:
        # Force every datetime out of the wire as UTC with an
        # explicit ``Z`` suffix. See ``_iso_utc`` for rationale.
        return _iso_utc(dt)
