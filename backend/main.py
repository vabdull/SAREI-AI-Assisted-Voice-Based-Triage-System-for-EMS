"""FastAPI application entrypoint.

Builds the ``app`` object: configures logging, mounts the versioned API
router, sets up CORS and per-request access logging, and runs startup
work (database init, event-loop binding for the live pipeline, ASR model
preload) via the ``lifespan`` handler. Served as ``backend.main:app``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend.ai.asr_runtime import StreamingAsrService
from backend.api.router import router as api_router
from backend.core.config import get_settings
from backend.core.logging import configure_logging
from backend.db.init_db import init_database

settings = get_settings()
configure_logging()

_access_logger = logging.getLogger("backend.access")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown work run once around the app's lifetime.

    On startup: re-apply logging, create/migrate the database, bind the
    main event loop to the live-pipeline services, and preload the ASR
    model. Everything after ``yield`` would run on shutdown.
    """
    # Re-apply our logging config on startup so any handlers uvicorn
    # attaches after importing the module (e.g. its own uvicorn.access
    # logger reconfigured from LOGGING_CONFIG) don't mask ours.
    configure_logging()
    init_database()

    # Bind the FastAPI main event loop to the services that need to
    # schedule coroutines from sync HTTP worker threads. Before this
    # the /live-chunk handler (sync def) silently dropped every
    # broadcast and LLM enrichment schedule because
    # ``asyncio.get_running_loop`` raises in a worker thread.
    import asyncio as _asyncio

    from backend.services.case_state_service import get_case_state_service
    from backend.services.enrichment_service import get_enrichment_service

    main_loop = _asyncio.get_running_loop()
    state_service = get_case_state_service()
    state_service.bind_loop(main_loop)
    # ``get_case_state_service`` already wires the enrichment callback.
    enrichment = get_enrichment_service()
    enrichment.bind_loop(main_loop)

    try:
        # Warm the ASR model at startup so the first live chunk is fast.
        StreamingAsrService()._load_model()
    except Exception:
        # Keep the app bootable even if ASR dependencies/model are
        # unavailable; the inference endpoint surfaces the concrete error
        # per request. We log here so a broken ASR boot is visible instead
        # of failing silently.
        logger.warning("ASR model preload failed at startup", exc_info=True)
    yield


app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Also allow any localhost/127.0.0.1 port so the dev frontend works
    # regardless of which port Vite picks, without listing each one.
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def request_logger(request: Request, call_next):
    """Log every HTTP request's method, path, status, and latency."""
    start = time.perf_counter()
    path = request.url.path
    method = request.method
    try:
        response = await call_next(request)
    except Exception:
        _access_logger.exception("%s %s -> exception", method, path)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    _access_logger.info(
        "%s %s -> %s in %.1fms", method, path, response.status_code, elapsed_ms
    )
    return response


app.include_router(api_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Lightweight liveness probe used by scripts and uptime checks."""
    return {"status": "ok", "service": settings.app_name}
