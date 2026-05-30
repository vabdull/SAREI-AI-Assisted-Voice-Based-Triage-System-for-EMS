"""SQLAlchemy ORM models — the database schema for the EMS platform.

Five tables back the whole application:

- ``User``            account + role (dispatcher / medic / hospital / admin)
- ``Case``            one emergency case: transcript, extracted fields,
                      triage result, and ambulance/hospital routing state
- ``TranscriptSegment`` individual transcribed utterances tied to a case
- ``CallRecording``   metadata for the saved audio recording of a call
- ``AuditLog``        trail of sensitive administrative actions

The enums below (``UserRole``, ``CaseStatus``, ``TriagePriority``,
``CallRecordingStatus``) constrain the allowed values for key columns.
"""

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ─────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    dispatcher = "dispatcher"
    medic = "medic"
    admin = "admin"
    hospital = "hospital"


class CaseStatus(str, enum.Enum):
    active = "active"
    en_route = "en_route"
    at_scene = "at_scene"
    transporting = "transporting"
    at_hospital = "at_hospital"
    closed = "closed"


class TriagePriority(str, enum.Enum):
    red = "red"
    yellow = "yellow"
    green = "green"
    black = "black"


class CallRecordingStatus(str, enum.Enum):
    uploading = "uploading"
    ready = "ready"
    processing = "processing"
    processed = "processed"
    error = "error"


# ── Models ────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(512))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.dispatcher)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[CaseStatus] = mapped_column(Enum(CaseStatus), default=CaseStatus.active)

    # How the case originated: ``voice`` (AI-assisted live call — the
    # default for every existing/auto-created case) or ``manual`` (a
    # dispatcher typed it in via the Manual Case Entry form). Stored as
    # a plain string (not an Enum) so adding future sources never needs
    # a DB enum migration. Lets every portal distinguish AI-voice cases
    # from manually-entered ones.
    source: Mapped[str] = mapped_column(
        String(20), default="voice", server_default="voice", nullable=False,
    )
    triage_priority: Mapped[TriagePriority | None] = mapped_column(
        Enum(TriagePriority), nullable=True,
    )

    patient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    patient_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    patient_gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    chief_complaint: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Number of injured/affected persons as extracted by the live
    # NLP pipeline (``CaseLiveState.patient_count``). Mirrored from
    # the canonical state so the medic/hospital portals can surface
    # "عدد المصابين" without re-querying the live store. Monotonic
    # by construction: see ``_merge_patient_count`` in
    # case_state_service.py — we never silently downgrade a higher
    # earlier count.
    patient_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Single canonical location payload. Matches backend.app.schemas.location.PatientLocation.
    # Serialized as JSON; None when unknown.
    patient_location: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Structured clinical fields captured by the Manual Case Entry form
    # that don't have a dedicated column (emergency_type, consciousness,
    # breathing, symptoms, severity, ...). Kept as JSON so the manual
    # report retains full fidelity without bloating the schema. ``None``
    # for voice/AI cases.
    manual_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    dispatcher_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigned_medic_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True,
    )
    assigned_hospital_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True,
    )

    ai_triage_suggestion: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # When the AMBULANCE side pressed "إنهاء الحالة". This is a soft
    # acknowledgement from the medic that their part is done — it
    # does NOT close the case overall. The medic portal hides cases
    # where this timestamp is set so the medic's active list reflects
    # only patients they still need to act on; the hospital portal
    # ignores this column and keeps showing the case until *it*
    # presses its own button (which sets status=closed).
    medic_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    dispatcher: Mapped[User] = relationship(
        "User", foreign_keys=[dispatcher_id],
    )
    assigned_medic: Mapped[User | None] = relationship(
        "User", foreign_keys=[assigned_medic_id],
    )
    assigned_hospital: Mapped[User | None] = relationship(
        "User", foreign_keys=[assigned_hospital_id],
    )
    transcript_segments: Mapped[list["TranscriptSegment"]] = relationship(
        "TranscriptSegment", back_populates="case",
    )
    recordings: Mapped[list["CallRecording"]] = relationship(
        "CallRecording", back_populates="case",
    )


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    speaker: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[float] = mapped_column(Float)
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    case: Mapped[Case] = relationship("Case", back_populates="transcript_segments")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CallRecording(Base):
    __tablename__ = "call_recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"))
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    original_filename: Mapped[str] = mapped_column(String(512))
    secure_filename: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(1024))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[CallRecordingStatus] = mapped_column(
        Enum(CallRecordingStatus), default=CallRecordingStatus.uploading,
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    case: Mapped[Case] = relationship("Case", back_populates="recordings")
