"""Top-level API router.

Assembles every feature router under a single ``/api/v1`` tree, each
mounted at its own prefix (auth, cases, dispatcher, inference, realtime,
triage WS, admin, ambulance, hospital). This is the single place that
defines the public URL surface of the backend.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.api.v1.admin import router as admin_router
from backend.app.api.v1.ambulance import router as ambulance_router
from backend.app.api.v1.auth import router as auth_router
from backend.app.api.v1.cases import router as cases_router
from backend.app.api.v1.dispatcher import router as dispatcher_router
from backend.app.api.v1.hospital import router as hospital_router
from backend.app.api.v1.inference import router as inference_router
from backend.app.api.v1.realtime import router as realtime_router
from backend.app.api.v1.triage_ws import router as triage_ws_router

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["Auth"])
router.include_router(cases_router, prefix="/cases", tags=["Cases"])
router.include_router(dispatcher_router, prefix="/dispatcher", tags=["Dispatcher"])
router.include_router(inference_router, prefix="/inference", tags=["Inference"])
router.include_router(realtime_router, prefix="/realtime", tags=["Realtime"])
router.include_router(triage_ws_router, prefix="/triage", tags=["Triage"])
router.include_router(admin_router, prefix="/admin", tags=["Admin"])
router.include_router(ambulance_router, prefix="/ambulance", tags=["Ambulance"])
router.include_router(hospital_router, prefix="/hospital", tags=["Hospital"])
