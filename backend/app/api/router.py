"""Top-level API router.

Mounts the versioned API under ``/api`` so every endpoint lives at
``/api/v1/...``. New API versions would be added here alongside ``v1``.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.api.v1.router import router as v1_router

router = APIRouter(prefix="/api")
router.include_router(v1_router, prefix="/v1")
