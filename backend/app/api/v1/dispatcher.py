"""Dispatcher endpoints: confirm/dispatch a case and upload its recording.

``dispatch_case`` is the backend half of the dispatcher's "Confirm Case"
action: it assigns a medic and hospital, advances the case status, and is
idempotent. ``ensure_hospital_assignment`` is shared with the ambulance
flow so both produce identical state.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_user, require_role
from backend.app.core.config import ROOT_DIR, get_settings
from backend.app.core.paths import to_posix_storage_path
from backend.app.db.models import (
    CallRecording,
    CallRecordingStatus,
    Case,
    CaseStatus,
    TriagePriority,
    User,
    UserRole,
)
from backend.app.db.session import get_db
from backend.app.schemas.cases import CaseRead

logger = logging.getLogger(__name__)

router = APIRouter()


_VALID_TRIAGE = {member.value for member in TriagePriority}


class CaseDispatchRequest(BaseModel):
    """Payload for the dispatcher "confirm + send" action.

    The dispatcher UI builds this from the current local triage badge
    and notes field. All fields are optional so the endpoint can also
    be called as a pure assignment trigger ("dispatch as-is").

    ``chief_complaint`` carries the dispatcher's final list of
    highlighted symptoms/keywords. It is also mirrored continuously
    by ``case_state_service`` during the call (from
    ``CaseLiveState.keywords``) but accepting it here lets the
    dispatcher freeze the value at confirm time even if the live
    pipeline hasn't flushed a final mirror yet.

    ``include_hospital`` lets the dispatcher choose between the two
    UI buttons:

    * ``True`` (default, back-compat) — also auto-assigns a hospital.
      Equivalent to "Send to Ambulance + Hospital".
    * ``False`` — only assigns a medic. Equivalent to
      "Send to Ambulance". The medic can later notify a hospital via
      ``POST /cases/{case_id}/send-to-hospital``.
    """

    triage_priority: Optional[str] = None
    notes: Optional[str] = None
    chief_complaint: Optional[str] = None
    include_hospital: bool = True

    @field_validator("triage_priority")
    @classmethod
    def _validate_triage(cls, v):
        if v is None:
            return v
        if v not in _VALID_TRIAGE:
            raise ValueError(
                f"Invalid triage_priority {v!r}. Allowed: {sorted(_VALID_TRIAGE)}"
            )
        return v


def save_upload_file_to_secure_recording(
    db: Session,
    case: Case,
    upload: UploadFile,
    uploaded_by: User,
) -> CallRecording:
    """Persist an uploaded audio file to disk and create its DB row."""
    settings = get_settings()
    storage_root = Path(ROOT_DIR / settings.recording_storage_dir)
    case_dir = storage_root / str(case.id)
    case_dir.mkdir(parents=True, exist_ok=True)

    raw = upload.file.read()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    logger.info(
        "Received audio upload for case %s: filename=%s mime=%s bytes=%d header=%s",
        case.id,
        upload.filename or "unknown",
        upload.content_type or "unknown",
        len(raw),
        raw[:16].hex(),
    )

    digest = hashlib.sha256(raw).hexdigest()[:16]
    suffix = Path(upload.filename or "recording.wav").suffix or ".wav"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    secure_name = f"{ts}_{digest}{suffix}"
    dest = case_dir / secure_name
    dest.write_bytes(raw)

    recording = CallRecording(
        case_id=case.id,
        uploaded_by_id=uploaded_by.id,
        original_filename=upload.filename or "unknown",
        secure_filename=secure_name,
        storage_path=to_posix_storage_path(str(dest)),
        file_size_bytes=len(raw),
        mime_type=upload.content_type or "audio/wav",
        status=CallRecordingStatus.ready,
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)
    return recording


@router.post("/upload-recording")
def upload_recording(
    case_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Store an uploaded call recording for a case (any authenticated user)."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    recording = save_upload_file_to_secure_recording(db, case, file, current_user)
    return {
        "id": recording.id,
        "case_id": recording.case_id,
        "original_filename": recording.original_filename,
        "secure_filename": recording.secure_filename,
        "file_size_bytes": recording.file_size_bytes,
        "mime_type": recording.mime_type,
        "status": recording.status.value if hasattr(recording.status, "value") else recording.status,
        "created_at": recording.created_at.isoformat(),
    }


def _pick_first_active(db: Session, role: UserRole) -> User | None:
    """Pick the first active user with ``role``.

    Selection is deterministic (lowest ``id``) rather than load-balanced,
    so repeated confirmations of the same case always resolve to the same
    assignee. Load balancing is intentionally out of scope.
    """
    return (
        db.query(User)
        .filter(User.role == role, User.is_active.is_(True))
        .order_by(User.id.asc())
        .first()
    )


def ensure_hospital_assignment(db: Session, case: Case) -> bool:
    """Assign a hospital to ``case`` if one isn't assigned yet.

    Single source of truth shared by the dispatcher "Send to Ambulance
    + Hospital" path and the ambulance "Send to Hospital" path so
    both produce exactly the same DB state and the same idempotency
    guarantees.

    Returns ``True`` when the case was modified (a hospital was just
    assigned) and ``False`` when it was already assigned or when no
    active hospital user exists. **Does not commit** — the caller is
    responsible so multiple mutations land in the same transaction.
    """
    if case.assigned_hospital_id is not None:
        return False
    hospital = _pick_first_active(db, UserRole.hospital)
    if hospital is None:
        logger.warning(
            "Case %s: no active hospital user registered — case will "
            "not be visible to the hospital portal until one is.",
            case.id,
        )
        return False
    case.assigned_hospital_id = hospital.id
    logger.info("Case %s: assigned hospital user=%s", case.id, hospital.id)
    return True


@router.post("/cases/{case_id}/dispatch", response_model=CaseRead)
def dispatch_case(
    case_id: int,
    payload: CaseDispatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("dispatcher", "admin")),
) -> Case:
    """Confirm a case and dispatch it to a medic + hospital.

    This is the backend half of the dispatcher's "Confirm Case" action,
    which makes the case visible to the ambulance and hospital portals.

    Behavior:

    * Persists ``triage_priority`` and ``notes`` from the request body
      (both optional; only set when provided).
    * If ``assigned_medic_id`` is null, auto-picks the first active
      medic. Same for ``assigned_hospital_id``.
    * Transitions ``status`` from ``active`` to ``en_route`` once a
      medic has been assigned so the hospital portal (which filters on
      en_route/transporting/at_hospital) immediately sees the case.
    * Idempotent: a second call with the same body returns the
      already-dispatched case without re-assigning or duplicating
      anything. This protects against a double click or a network retry
      creating duplicate assignments.
    * Raises 422 when no active medic exists in the system so the UI
      can surface a clear error instead of silently "succeeding" with
      no recipient.
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Case not found"
        )

    # Authorize: dispatcher who created the case, or admin.
    is_admin = current_user.role == UserRole.admin
    if not is_admin and case.dispatcher_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the case's dispatcher (or an admin) can dispatch it",
        )

    already_dispatched = (
        case.assigned_medic_id is not None
        and case.status != CaseStatus.active
    )

    changed = False

    # Persist triage / notes regardless of dispatch state — dispatcher
    # may correct them on a re-confirm without re-assigning.
    if payload.triage_priority is not None and (
        case.triage_priority is None
        or case.triage_priority.value != payload.triage_priority
    ):
        case.triage_priority = TriagePriority(payload.triage_priority)
        changed = True
    if payload.notes is not None and payload.notes != (case.notes or ""):
        case.notes = payload.notes
        changed = True
    if (
        payload.chief_complaint is not None
        and payload.chief_complaint.strip()
        and payload.chief_complaint != (case.chief_complaint or "")
    ):
        case.chief_complaint = payload.chief_complaint
        changed = True

    if already_dispatched:
        logger.info(
            "Case %s already dispatched (medic=%s hospital=%s status=%s); "
            "refreshed metadata only.",
            case.id,
            case.assigned_medic_id,
            case.assigned_hospital_id,
            case.status,
        )
        if changed:
            db.commit()
            db.refresh(case)
        return case

    # Pick an available medic. A hospital is optional — there are
    # deployments without a hospital portal user — but the medic is
    # required: without one there's no recipient to dispatch to.
    if case.assigned_medic_id is None:
        medic = _pick_first_active(db, UserRole.medic)
        if medic is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "No active medic is registered in the system. Register "
                    "a user with role=medic before dispatching cases."
                ),
            )
        case.assigned_medic_id = medic.id
        changed = True

    if payload.include_hospital and ensure_hospital_assignment(db, case):
        changed = True

    if case.status == CaseStatus.active:
        case.status = CaseStatus.en_route
        changed = True

    if changed:
        db.commit()
        db.refresh(case)

    logger.info(
        "Case %s dispatched by user=%s medic=%s hospital=%s triage=%s",
        case.id,
        current_user.id,
        case.assigned_medic_id,
        case.assigned_hospital_id,
        case.triage_priority,
    )
    return case
