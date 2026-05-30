"""Audit logging for security- and compliance-relevant actions.

Provides a single helper to persist an ``AuditLog`` row describing who
performed which action on which resource, with optional details and the
caller's IP. Used primarily by the admin user-management endpoints.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.app.db.models import AuditLog

logger = logging.getLogger(__name__)


def log_audit_event(
    db: Session,
    user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Record and commit a single audit-trail entry, returning the row.

    ``action`` is a short verb (e.g. ``"user.create"``); ``resource_type``
    /``resource_id`` identify the target; ``details`` is an optional JSON
    object with action-specific context.
    """
    entry = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=ip_address,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    logger.info(
        "audit | user=%s action=%s resource=%s/%s",
        user_id,
        action,
        resource_type,
        resource_id,
    )
    return entry
