"""
WebSocket endpoint for real-time triage events.

URL: ``/api/v1/triage/ws/{case_id}?token=<JWT>``

After the refactor this route is thin: it authenticates, authorises,
registers the websocket with ``case_state_service``, relays client
messages, and hands preview chunks to ``fast_decision_service``.

Server-to-client messages are emitted by ``case_state_service``:

* ``{"type": "live_state", ...}`` — canonical ``CanonicalLivePayload``;
  the single payload the frontend should consume going forward.
* ``{"type": "triage_update", ...}`` — legacy shape kept alongside
  ``live_state`` for the existing UI to keep working unchanged during
  the transition.

Client-to-server messages are unchanged:

* ``{"type": "chunk", "text": "<utterance>", "provisional": bool}``
* ``{"type": "reset"}``
* ``{"type": "ping"}``
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError
from sqlalchemy.orm import Session

from backend.core.security import decode_access_token
from backend.db.models import Case, User, UserRole
from backend.db.session import get_db
from backend.services.case_state_service import get_case_state_service
from backend.services.fast_decision_service import get_fast_decision_service
from backend.services.recording_buffer_service import (
    get_recording_buffer_service,
)
from backend.services.transcript_service import get_transcript_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _authenticate(token: str, db: Session) -> User | None:
    try:
        payload = decode_access_token(token)
    except JWTError:
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return db.query(User).filter(User.id == uid).first()


def _user_can_view_case(user: User, case: Case) -> bool:
    role = getattr(user, "role", None)
    if role is None:
        return False
    role_name = role.value if hasattr(role, "value") else str(role)
    if role_name in {UserRole.admin.value, UserRole.dispatcher.value}:
        return True
    if role_name == UserRole.medic.value:
        return getattr(case, "ambulance_user_id", None) == user.id
    if role_name == UserRole.hospital.value:
        return getattr(case, "hospital_user_id", None) == user.id
    return False


@router.websocket("/ws/{case_id}")
async def triage_websocket(
    websocket: WebSocket,
    case_id: int,
    token: str = Query(..., description="JWT access token"),
) -> None:
    db: Session = next(get_db())
    state_service = get_case_state_service()
    fast_service = get_fast_decision_service()
    transcript_service = get_transcript_service()
    connected = False

    try:
        user = _authenticate(token, db)
        if user is None:
            await websocket.close(code=4401)
            return

        case = db.query(Case).filter(Case.id == case_id).first()
        if case is None:
            await websocket.close(code=4404)
            return

        if not _user_can_view_case(user, case):
            await websocket.close(code=4403)
            return

        await websocket.accept()
        await state_service.connect(case_id, websocket)
        connected = True

        # On connect, send the current canonical snapshot so clients
        # joining mid-call immediately see the accumulated state.
        try:
            payload = state_service.snapshot_payload(case_id)
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "case_id": case_id,
                    "state": payload.state.model_dump(mode="json"),
                    "result": (
                        payload.state.fast_triage.model_dump(mode="json")
                        if payload.state.fast_triage is not None
                        else None
                    ),
                }
            )
        except Exception:
            logger.exception("snapshot send failed case=%s", case_id)

        while True:
            raw = await websocket.receive_text()
            try:
                message: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "detail": "Invalid JSON payload"}
                )
                continue

            msg_type = message.get("type")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if msg_type == "reset":
                state_service.reset(case_id)
                transcript_service.reset(case_id)
                get_recording_buffer_service().reset(case_id)
                await websocket.send_json(
                    {"type": "triage_reset", "case_id": case_id}
                )
                continue

            if msg_type == "chunk":
                chunk_text = (message.get("text") or "").strip()
                if not chunk_text:
                    continue
                provisional = bool(message.get("provisional"))
                preview_transcript = (message.get("preview_transcript") or "").strip()

                try:
                    if provisional:
                        # Preview path: do NOT mutate the authoritative
                        # transcript or revision. Compute a fast result
                        # bound to the CURRENT revision and broadcast a
                        # provisional payload.
                        current_state = state_service.get(case_id)
                        revision = current_state.transcript_revision
                        effective_transcript = (
                            preview_transcript
                            or current_state.transcript_text
                            or chunk_text
                        )
                        fast = fast_service.process(
                            case_id=case_id,
                            revision=revision,
                            chunk_text=chunk_text,
                            transcript_text=effective_transcript,
                            provisional=True,
                        )
                        state_service.apply_fast(
                            case_id=case_id, fast=fast, provisional=True
                        )
                    else:
                        merge_result = transcript_service.ingest_chunk(
                            db=db, case_id=case_id, chunk_text=chunk_text
                        )
                        state_service.apply_transcript_update(
                            case_id=case_id,
                            transcript_text=merge_result.text,
                            revision=merge_result.revision,
                        )
                        fast = fast_service.process(
                            case_id=case_id,
                            revision=merge_result.revision,
                            chunk_text=chunk_text,
                            transcript_text=merge_result.text,
                            provisional=False,
                        )
                        state_service.apply_fast(
                            case_id=case_id, fast=fast, provisional=False
                        )
                except Exception:
                    logger.exception(
                        "triage_ws: fast path failed case=%s provisional=%s",
                        case_id,
                        provisional,
                    )
                    await websocket.send_json(
                        {"type": "error", "detail": "triage pipeline error"}
                    )
                continue

            await websocket.send_json(
                {"type": "error", "detail": f"Unknown message type: {msg_type!r}"}
            )

    except WebSocketDisconnect:
        logger.info("Triage WS disconnect case=%s", case_id)
    except Exception:
        logger.exception("Triage WS fatal error case=%s", case_id)
        try:
            await websocket.close(code=1011)
        except Exception:
            # The socket is already broken; a failed close is expected
            # and harmless. Logged at debug for diagnosability only.
            logger.debug("Triage WS close after error also failed case=%s", case_id)
    finally:
        if connected:
            await state_service.disconnect(case_id, websocket)
        db.close()
