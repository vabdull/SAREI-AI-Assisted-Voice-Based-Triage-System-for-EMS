"""Security primitives: password hashing and JWT access tokens.

Passwords are hashed with passlib; login tokens are signed JWTs. Both
``pbkdf2_sha256`` and ``bcrypt`` schemes are registered so existing
bcrypt hashes still verify while new hashes use the default scheme.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

from backend.app.core.config import get_settings

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return a one-way hash suitable for storing in the database."""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a stored hash."""
    # Treat any verification error (e.g. a malformed/legacy hash) as a
    # failed login rather than a crash. We log it so corrupt credential
    # data is diagnosable instead of silently always-denying.
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        logger.warning("Password verification raised; treating as failure", exc_info=True)
        return False


def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None,
) -> str:
    """Sign a JWT carrying ``data`` plus an expiry claim.

    Defaults to the configured ``access_token_expire_minutes`` lifetime.
    """
    settings = get_settings()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.access_token_expire_minutes)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Verify a JWT's signature/expiry and return its claims.

    Raises ``jose.JWTError`` if the token is invalid or expired.
    """
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
