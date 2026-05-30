"""Core case CRUD endpoints.

Create, list, fetch, and update emergency cases, plus append transcript
segments and list a case's recordings. Lifecycle transitions (dispatch,
hospital hand-off, completion) live in the role-specific routers
(dispatcher, ambulance, hospital), not here.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_user, require_role
from backend.app.db.models import (
    CallRecording,
    Case,
    TranscriptSegment,
    TriagePriority,
    User,
)
from backend.app.db.session import get_db
from backend.app.schemas.cases import (
    CaseCreate,
    CaseRead,
    CaseUpdate,
    TranscriptSegmentCreateRequest,
    TranscriptSegmentRead,
)

router = APIRouter()


@router.get("/", response_model=list[CaseRead])
def list_cases(
    status_filter: Optional[str] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Case]:
    """List cases (any authenticated user), optionally filtered by status."""
    query = db.query(Case)
    if status_filter:
        query = query.filter(Case.status == status_filter)
    return query.order_by(Case.created_at.desc()).offset(skip).limit(limit).all()


@router.post("/", response_model=CaseRead, status_code=status.HTTP_201_CREATED)
def create_case(
    payload: CaseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("dispatcher", "admin")),
) -> Case:
    """Create a case (dispatcher/admin) with a generated incident number."""
    case = Case(
        incident_number=uuid.uuid4().hex[:12].upper(),
        dispatcher_id=current_user.id,
        source=payload.source or "voice",
        patient_name=payload.patient_name,
        patient_age=payload.patient_age,
        patient_gender=payload.patient_gender,
        patient_count=payload.patient_count,
        chief_complaint=payload.chief_complaint,
        triage_priority=(
            TriagePriority(payload.triage_priority)
            if payload.triage_priority is not None
            else None
        ),
        patient_location=(
            payload.patient_location.model_dump(mode="json")
            if payload.patient_location is not None
            else None
        ),
        notes=payload.notes,
        manual_details=payload.manual_details,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.get("/{case_id}", response_model=CaseRead)
def get_case(
    case_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Case:
    """Fetch a single case by id (any authenticated user); 404 if missing."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )
    return case


# Lifecycle / assignment fields that must NEVER be writable through
# the generic case-edit endpoint. Each has a dedicated, role-guarded
# route that is the single source of truth for that transition:
#
#   * ``status`` / ``assigned_*`` on dispatch  → POST /dispatcher/cases/{id}/dispatch
#   * ``status`` during the run                → PATCH /ambulance/cases/{id}/status
#   * ``status = closed``                      → POST /hospital/cases/{id}/complete
#
# Allowing them here was a dispatch-gate bypass: any authenticated
# user could PATCH ``status`` off ``active`` (or set ``assigned_medic_id``)
# and make a case the dispatcher was still handling appear in the
# medic/ambulance portal mid-call. This endpoint is for editing
# patient/scene metadata only.
_PROTECTED_CASE_FIELDS = {"status", "assigned_medic_id", "assigned_hospital_id"}


@router.patch("/{case_id}", response_model=CaseRead)
def update_case(
    case_id: int,
    payload: CaseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Case:
    """Update editable case metadata only; lifecycle changes use other routes."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    update_data = payload.model_dump(exclude_unset=True, mode="json")

    blocked = _PROTECTED_CASE_FIELDS.intersection(update_data)
    if blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Fields "
                f"{sorted(blocked)} cannot be changed through this "
                "endpoint. Dispatch a case via "
                "POST /dispatcher/cases/{id}/dispatch, update its run "
                "status via PATCH /ambulance/cases/{id}/status, and "
                "close it via POST /hospital/cases/{id}/complete."
            ),
        )

    for field, value in update_data.items():
        setattr(case, field, value)

    db.commit()
    db.refresh(case)
    return case


@router.post("/{case_id}/transcript", response_model=TranscriptSegmentRead, status_code=status.HTTP_201_CREATED)
def add_transcript_segment(
    case_id: int,
    payload: TranscriptSegmentCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TranscriptSegment:
    """Append a transcript segment to a case (any authenticated user)."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    segment = TranscriptSegment(
        case_id=case_id,
        speaker=payload.speaker,
        text=payload.text,
        timestamp=0.0,
        is_ai_generated=payload.is_ai_generated,
        confidence=payload.confidence,
    )
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment


@router.get("/{case_id}/recordings", response_model=list[dict])
def list_recordings(
    case_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    """List a case's call recordings (any authenticated user)."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    recordings = (
        db.query(CallRecording)
        .filter(CallRecording.case_id == case_id)
        .order_by(CallRecording.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "original_filename": r.original_filename,
            "secure_filename": r.secure_filename,
            "file_size_bytes": r.file_size_bytes,
            "mime_type": r.mime_type,
            "status": r.status.value if hasattr(r.status, "value") else r.status,
            "duration_seconds": r.duration_seconds,
            "created_at": r.created_at.isoformat(),
        }
        for r in recordings
    ]
