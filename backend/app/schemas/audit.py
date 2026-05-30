"""Response model for the admin audit trail."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class AuditLogRead(BaseModel):
    """A single audit record: who did what action on which resource, when.

    ``details`` holds an action-specific JSON object (e.g. changed fields)
    and ``ip_address`` records the request origin for traceability.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: Optional[int] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    details: dict[str, Any] = {}
    ip_address: Optional[str] = None
    created_at: datetime
