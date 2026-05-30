"""Database engine and session management.

Creates the SQLAlchemy engine/session factory and exposes ``get_db`` as
the FastAPI dependency that hands each request a session and guarantees
it is closed afterwards.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    # SQLite ties a connection to the thread that created it by default.
    # FastAPI serves requests from a thread pool, so we disable that check
    # to let sessions be used across worker threads safely.
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session]:
    """FastAPI dependency: yield a DB session, then always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
