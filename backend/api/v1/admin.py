"""Admin-only endpoints for user management and the audit trail.

All routes require the ``admin`` role. User mutations (create/update/
delete) are recorded via the audit service and protected by safety
guards (e.g. an admin cannot demote/delete themselves, and the last
active admin cannot be removed).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.api.deps import require_role
from backend.core.security import hash_password
from backend.db.models import AuditLog, User, UserRole
from backend.db.session import get_db
from backend.schemas.audit import AuditLogRead
from backend.schemas.auth import (
    AdminUserCreate,
    AdminUserUpdate,
    UserRead,
)
from backend.services.audit_service import log_audit_event

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/audit-logs", response_model=list[AuditLogRead])
def list_audit_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> list[AuditLog]:
    """Return the most recent audit-log entries (admin only)."""
    return (
        db.query(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/users", response_model=list[UserRead])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> list[User]:
    """Return all user accounts, newest first (admin only)."""
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post(
    "/users",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    payload: AdminUserCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> User:
    """Create a user account (admin only) and record an audit event."""
    existing = (
        db.query(User)
        .filter(or_(User.username == payload.username, User.email == payload.email))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="اسم المستخدم أو البريد الإلكتروني مُسجّل بالفعل",
        )

    user = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=UserRole(payload.role),
        is_active=payload.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    log_audit_event(
        db,
        user_id=current_user.id,
        action="user.create",
        resource_type="user",
        resource_id=str(user.id),
        details={"username": user.username, "role": user.role.value},
        ip_address=_client_ip(request),
    )
    return user


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> User:
    """Update a user (admin only).

    Applies only the provided fields. Guards prevent an admin from
    demoting or deactivating their own account. Records an audit event.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="المستخدم غير موجود",
        )

    changed: dict = {}

    if payload.email is not None and payload.email != user.email:
        clash = (
            db.query(User)
            .filter(User.email == payload.email, User.id != user_id)
            .first()
        )
        if clash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="البريد الإلكتروني مُستخدم من قبل حساب آخر",
            )
        user.email = payload.email
        changed["email"] = payload.email

    if payload.full_name is not None and payload.full_name != user.full_name:
        user.full_name = payload.full_name
        changed["full_name"] = payload.full_name

    if payload.role is not None and payload.role != user.role.value:
        # Guard: an admin must not demote themselves (lockout risk) and
        # the LAST active admin must not lose the admin role.
        if user.id == current_user.id and payload.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="لا يمكنك تغيير دور حسابك الخاص",
            )
        if user.role == UserRole.admin and payload.role != "admin":
            _guard_last_admin(db, user_id)
        user.role = UserRole(payload.role)
        changed["role"] = payload.role

    if payload.is_active is not None and payload.is_active != user.is_active:
        if user.id == current_user.id and not payload.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="لا يمكنك تعطيل حسابك الخاص",
            )
        if (
            user.role == UserRole.admin
            and not payload.is_active
        ):
            _guard_last_admin(db, user_id)
        user.is_active = payload.is_active
        changed["is_active"] = payload.is_active

    if payload.password:
        user.hashed_password = hash_password(payload.password)
        changed["password"] = "***"

    if changed:
        db.commit()
        db.refresh(user)
        log_audit_event(
            db,
            user_id=current_user.id,
            action="user.update",
            resource_type="user",
            resource_id=str(user.id),
            details={"fields": list(changed.keys())},
            ip_address=_client_ip(request),
        )
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> None:
    """Delete a user (admin only).

    Guards prevent self-deletion and removal of the last active admin.
    Records an audit event.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="المستخدم غير موجود",
        )
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="لا يمكنك حذف حسابك الخاص",
        )
    if user.role == UserRole.admin:
        _guard_last_admin(db, user_id)

    username = user.username
    db.delete(user)
    db.commit()
    log_audit_event(
        db,
        user_id=current_user.id,
        action="user.delete",
        resource_type="user",
        resource_id=str(user_id),
        details={"username": username},
        ip_address=_client_ip(request),
    )
    return None


def _guard_last_admin(db: Session, excluding_user_id: int) -> None:
    """Block actions that would leave zero active admins."""
    remaining_admins = (
        db.query(User)
        .filter(
            User.role == UserRole.admin,
            User.is_active.is_(True),
            User.id != excluding_user_id,
        )
        .count()
    )
    if remaining_admins == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="لا يمكن إزالة آخر مدير نشط في النظام",
        )
