"""Declarative base shared by every ORM model in ``db/models.py``."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
