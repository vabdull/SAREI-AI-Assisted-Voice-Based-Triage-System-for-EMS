"""Pydantic request/response models for authentication and user accounts.

Covers registration, login, the public user view, JWT token payloads, and
the admin-only create/update variants. Validators normalise input
(lower-cased usernames/emails, trimmed names) and enforce the role/password
rules at the API boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

# Allowed account roles; mirrors the ``UserRole`` enum in db/models.py.
VALID_ROLES = {"dispatcher", "medic", "hospital", "admin"}


class UserCreate(BaseModel):
    """Public self-registration payload."""

    username: str
    email: EmailStr
    full_name: str
    password: str
    role: str = "dispatcher"

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("full_name")
    @classmethod
    def strip_full_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class AdminUserCreate(UserCreate):
    """Admin-side user creation. Adds optional ``is_active`` on top of
    the standard registration payload."""

    is_active: bool = True


class AdminUserUpdate(BaseModel):
    """Partial update of a user by an admin. Every field is optional;
    only provided fields are changed. ``password`` re-hashes the
    credential when present and non-empty."""

    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().lower() if v else v

    @field_validator("full_name")
    @classmethod
    def strip_full_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("full_name cannot be empty")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class UserLogin(BaseModel):
    """Login credentials."""

    username: str
    password: str


class UserRead(BaseModel):
    """Safe user representation returned to clients (no password hash)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """JWT returned on successful login."""

    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    """Decoded JWT claims: ``sub`` (user id) and ``exp`` (expiry)."""

    sub: str | None = None
    exp: int | None = None
