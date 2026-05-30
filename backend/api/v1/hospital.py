"""Hospital portal endpoints.

Lets a hospital view its queue of incoming cases, fetch a single case's
detail, and mark a case complete. A case is "incoming" from the moment
the dispatcher confirms it (``en_route``) through arrival, so the
hospital can prepare before the ambulance reports movement.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.api.deps import get_current_user, require_role
from backend.db.models import Case, CaseStatus, User, UserRole
from backend.db.session import get_db
from backend.schemas.cases import CaseRead

logger = logging.getLogger(__name__)

router = APIRouter()


# Status values that the hospital portal treats as "incoming". A case is
# shown from the moment the dispatcher confirms it (``en_route``) so the
# hospital sees it during the early minutes of the call, not only once
# the ambulance reports it is moving.
_INCOMING_STATUSES = ("en_route", "at_scene", "transporting", "at_hospital")


@router.get("/incoming", response_model=list[CaseRead])
def incoming_cases(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Case]:
    """Hospital-side incoming case list.

    Shared-queue model: every active hospital user sees every case
    that has actually been **routed to a hospital** — i.e. has a
    non-null ``assigned_hospital_id`` AND is in an "incoming"
    status. The dispatcher's choice of *which* hospital account is
    treated as a hint (all hospital accounts share the same queue),
    but the dispatcher's choice of *whether* to involve the
    hospital at all is honoured: "Send to Ambulance only" cases
    stay hidden until a medic forwards them (or the case reaches
    ``transporting`` and the auto-assign fires).

    Admin override: admins see every case in the system, including
    closed ones and pending/dispatched, so the portal works as a
    system-wide monitoring view.
    """
    query = db.query(Case)
    if current_user.role != UserRole.admin:
        query = query.filter(
            Case.status.in_(_INCOMING_STATUSES),
            Case.assigned_hospital_id.isnot(None),
        )
    return query.order_by(Case.created_at.desc()).all()


@router.get("/cases/{case_id}", response_model=CaseRead)
def get_case_detail(
    case_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Case:
    """Fetch full detail for one case (any authenticated user); 404 if missing."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )
    return case


@router.post("/cases/{case_id}/complete", response_model=CaseRead)
def complete_case(
    case_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("hospital", "admin")),
) -> Case:
    """Hospital finalises the case — sets status to ``closed``.

    Only the hospital portal (or an admin) can close a case. The
    ambulance portal never reaches ``closed`` so the ER team always
    has the final say on when the patient lifecycle is over.

    Idempotent: a second call returns the already-closed case
    unchanged. No duplicate updates, no exception.

    Authorisation: broadcast model — any active hospital user (or
    admin) may finalise any case. Access is gated by the role check
    on the route.
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    if case.status == CaseStatus.closed:
        # Already closed — idempotent short-circuit.
        return case

    case.status = CaseStatus.closed
    db.commit()
    db.refresh(case)
    logger.info(
        "Case %s completed by user=%s (role=%s)",
        case.id,
        current_user.id,
        current_user.role,
    )
    return case
