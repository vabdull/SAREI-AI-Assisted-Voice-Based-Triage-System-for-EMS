"""Ambulance (medic) portal endpoints.

Lets an assigned medic view their dispatched cases, advance the case
status through the on-scene/transporting lifecycle, hand a case off to a
hospital, and mark it complete. Status writes are validated against the
``CaseStatus`` enum before being persisted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_user, require_role
from backend.app.api.v1.dispatcher import ensure_hospital_assignment
from backend.app.db.models import Case, CaseStatus, User, UserRole
from backend.app.db.session import get_db
from backend.app.schemas.cases import CaseRead

logger = logging.getLogger(__name__)

router = APIRouter()


# Canonical set of status values the ``cases.status`` Enum column accepts.
# Writing a value outside this set corrupts the row: SQLAlchemy stores the
# raw string but then raises ``LookupError`` on every subsequent read, so
# we validate against this set before any status update.
_VALID_STATUSES = {member.value for member in CaseStatus}


class StatusUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        if v not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status {v!r}. Allowed: {sorted(_VALID_STATUSES)}"
            )
        return v


@router.get("/my-cases", response_model=list[CaseRead])
def my_cases(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Case]:
    """Ambulance-side case list.

    Broadcast model: every active medic sees every case that has
    actually been DISPATCHED to the ambulance side.

    Dispatch gate (applies to ALL roles, admin included):

    * ``status == active`` cases are NOT shown. ``active`` means the
      dispatcher is still on the call and has not sent the case to
      anyone yet — it belongs to the dispatcher portal only. A case
      enters the ambulance portal the moment dispatch flips it to
      ``en_route`` (see ``dispatcher.ensure_hospital_assignment`` /
      ``dispatch_case``).

    Additional filters for regular medics:

    * ``status == closed`` — case finalised by the hospital/admin,
      lifecycle is over for everyone.
    * ``medic_completed_at is not None`` — the ambulance side already
      pressed "إنهاء الحالة"; the case may still be alive in the
      hospital portal but the medic's part is done.

    Admin override: admins still see closed AND medic-completed cases
    (system-wide monitoring view) but, like everyone, do NOT see
    undispatched ``active`` cases here.
    """
    # Dispatch gate: require both ``status != active`` AND an assigned
    # medic. Requiring the medic assignment as well (not just the status)
    # ensures a case never appears here unless it actually went through
    # ``dispatch_case``, even if another code path changed the status.
    query = (
        db.query(Case)
        .filter(
            Case.status != CaseStatus.active,
            Case.assigned_medic_id.isnot(None),
        )
    )
    if current_user.role != UserRole.admin:
        query = query.filter(
            Case.status != CaseStatus.closed,
            Case.medic_completed_at.is_(None),
        )
    return query.order_by(Case.created_at.desc()).all()


# Status values a medic is allowed to write through the generic
# PATCH endpoint. Closing a case (status = closed) is a distinct
# lifecycle event that goes through ``/complete`` so both the
# ambulance and the hospital share the same idempotent closure path.
_MEDIC_ALLOWED_STATUSES = {
    CaseStatus.en_route,
    CaseStatus.at_scene,
    CaseStatus.transporting,
    CaseStatus.at_hospital,
}


@router.patch("/cases/{case_id}/status", response_model=CaseRead)
def update_case_status(
    case_id: int,
    payload: StatusUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("medic", "ambulance", "admin")),
) -> Case:
    target_status = CaseStatus(payload.status)

    if (
        current_user.role != UserRole.admin
        and target_status not in _MEDIC_ALLOWED_STATUSES
    ):
        # Defense-in-depth: even if a stale UI tries to PATCH closed
        # we reject it at the API edge. Hospital-side completion goes
        # through ``/hospital/cases/{id}/complete``.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Medics may not set status={target_status.value!r} "
                "via PATCH. Use POST /ambulance/cases/{id}/complete "
                "to close the case."
            ),
        )

    # Broadcast model: any active medic can update any case. We no
    # longer scope by ``assigned_medic_id`` because the assignment
    # column is now a "first responder" hint, not an access gate.
    # The lookup is a plain by-id read; access is gated by the role
    # check on the route (``medic | ambulance | admin``).
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    case.status = target_status

    # Auto-forward to a hospital the moment the ambulance starts
    # transporting. The medic clicking "جاري النقل" should be enough
    # to inform the ER — they shouldn't also have to click a second
    # button. The helper is idempotent: if the case already has a
    # hospital, this is a no-op. If no hospital user is registered
    # we log a warning but still let the status update succeed so the
    # ambulance flow isn't blocked by a missing hospital account.
    if target_status == CaseStatus.transporting and case.assigned_hospital_id is None:
        try:
            ensure_hospital_assignment(db, case)
        except Exception:  # noqa: BLE001
            # Defensive: hospital assignment is a nice-to-have on
            # this transition; never let it break the status PATCH.
            logger.exception(
                "Case %s: auto hospital-assignment on transporting failed",
                case.id,
            )

    db.commit()
    db.refresh(case)
    return case


# Statuses where it no longer makes sense to forward a case to a
# hospital — case is over or terminated.
_TERMINAL_STATUSES = {CaseStatus.closed}


@router.post("/cases/{case_id}/send-to-hospital", response_model=CaseRead)
def send_case_to_hospital(
    case_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("medic", "ambulance", "admin")),
) -> Case:
    """Forward an already-dispatched case to a hospital.

    Used by the ambulance portal when the dispatcher only sent the
    case "to ambulance" and the medic now wants to give the receiving
    ER a heads-up. Mirrors the hospital assignment side-effect of the
    dispatcher's "Send to Ambulance + Hospital" path so the resulting
    DB state is identical regardless of which actor initiated it.

    Idempotency: if the case already has an assigned hospital, returns
    the case unchanged (no duplicate notification, no error).

    Authorization: broadcast model — any active medic (or admin) may
    forward any case. Access is gated by the role check on the route.
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    if case.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Case is closed; cannot forward to hospital",
        )

    if case.assigned_hospital_id is not None:
        # Already sent — short-circuit, no side effects.
        return case

    assigned = ensure_hospital_assignment(db, case)
    if not assigned:
        # Helper returns False either because the hospital was already
        # set (caught above) or because no active hospital user
        # exists. Surface that distinctly so the UI can show a useful
        # message instead of a silent success.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No active hospital user is registered in the system. "
                "Register a user with role=hospital first."
            ),
        )

    db.commit()
    db.refresh(case)
    logger.info(
        "Case %s forwarded to hospital=%s by user=%s (role=%s)",
        case.id,
        case.assigned_hospital_id,
        current_user.id,
        current_user.role,
    )
    return case


@router.post("/cases/{case_id}/complete", response_model=CaseRead)
def complete_case(
    case_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("medic", "ambulance", "admin")),
) -> Case:
    """Ambulance marks "إنهاء الحالة" — soft completion.

    This is *not* the same as the hospital's complete endpoint. The
    ambulance side stamps ``medic_completed_at`` so the medic portal
    hides the case, but the case stays alive overall:

    * If the case has an assigned hospital, the hospital portal
      keeps seeing it until the hospital itself presses "إنهاء
      الحالة" (which sets ``status = closed``, the true close).
    * If the case has no assigned hospital, the case becomes
      invisible to all non-admin viewers (medic-hidden + no hospital
      to see it) — admins can still close it from the admin view.

    Idempotent: a second call returns the case unchanged.

    Authorisation: broadcast model — any active medic (or admin)
    may complete any case. Access is gated by the route role check.
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    if case.medic_completed_at is not None or case.status == CaseStatus.closed:
        # Already completed (or fully closed by hospital) — no-op.
        return case

    case.medic_completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(case)
    logger.info(
        "Case %s medic-completed by user=%s (role=%s); hospital_id=%s status=%s",
        case.id,
        current_user.id,
        current_user.role,
        case.assigned_hospital_id,
        case.status.value,
    )
    return case
