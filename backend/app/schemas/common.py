"""Reusable response models shared across API endpoints."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class MessageResponse(BaseModel):
    """Simple ``{"message": ...}`` body for status/confirmation responses."""

    message: str


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic page of ``items`` with total/page metadata for any item type."""

    items: list[T]
    total: int
    page: int
    page_size: int
