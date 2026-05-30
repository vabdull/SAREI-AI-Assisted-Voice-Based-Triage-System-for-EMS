"""WebSocket endpoint for low-latency live ASR preview.

Streams short audio windows from the dispatcher's browser to the ASR
model and pushes back rolling preview transcripts. A ``ConnectionManager``
tracks per-case subscribers for broadcast. This is the fast "preview"
path; committed transcription and analysis run over the HTTP/triage-WS
pipeline.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections import defaultdict
from typing import Any

import numpy as np
import soundfile as sf
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError
from sqlalchemy.orm import Session

from backend.app.ai.asr_runtime import StreamingAsrService
from backend.app.ai.audio_preprocessing import TARGET_SAMPLE_RATE
from backend.app.core.security import decode_access_token
from backend.app.db.models import User
from backend.app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

try:
    import librosa
except ImportError:
    librosa = None


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, case_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[case_id].add(websocket)

    async def disconnect(self, case_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections[case_id].discard(websocket)
            if case_id in self._connections and not self._connections[case_id]:
                del self._connections[case_id]

    async def broadcast(self, case_id: int, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._connections.get(case_id, set()))
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                # Send failure => client socket is gone; collect for pruning.
                logger.debug("Dropping dead realtime subscriber case=%s", case_id)
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections[case_id].discard(ws)


manager = ConnectionManager()
# Calls to the NeMo model are already serialised process-wide by
# ``StreamingAsrService._transcribe_lock`` (a ``threading.Lock``). We
# deliberately do not add a second asyncio-level lock here: doing so
# created a second queue that could starve preview transcription while a
# final transcription was in flight.
MAX_PREVIEW_BUFFER_SECONDS = 6
# Keep the preview window short so each transcribe() call processes a
# bounded amount of audio; long windows lead to slow re-transcription.
PREVIEW_TRANSCRIBE_WINDOW_SECONDS = 4


def _authenticate_ws_token(token: str, db: Session) -> User | None:
    try:
        payload = decode_access_token(token)
        user_id: str | None = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None
    return db.query(User).filter(User.id == int(user_id)).first()


def _resample_audio(audio: np.ndarray, original_sr: int, target_sr: int) -> np.ndarray:
    if original_sr == target_sr or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    if librosa is not None:
        return librosa.resample(audio, orig_sr=original_sr, target_sr=target_sr).astype(np.float32)

    ratio = target_sr / original_sr
    new_length = int(len(audio) * ratio)
    indices = np.linspace(0, len(audio) - 1, new_length)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def _wav_bytes_from_audio(audio: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


async def _transcribe_audio_buffer(
    asr: StreamingAsrService,
    audio_buffer: np.ndarray,
    chunk_index: int,
) -> str:
    wav_bytes = _wav_bytes_from_audio(audio_buffer, TARGET_SAMPLE_RATE)
    event = await asyncio.to_thread(asr.transcribe_chunk, wav_bytes, chunk_index, False)
    return event.text.strip()


@router.websocket("/ws/{case_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    case_id: int,
    token: str = Query(...),
) -> None:
    db: Session = next(get_db())
    try:
        user = _authenticate_ws_token(token, db)
        if user is None:
            await websocket.close(code=4401)
            return

        await manager.connect(case_id, websocket)
        await websocket.send_json({"type": "status", "message": "Connected. Ready for live audio."})

        asr = StreamingAsrService()
        active = False
        audio_encoding = "float32_pcm"
        mime_type: str | None = None
        chunk_count = 0
        client_sample_rate = 48000
        audio_buffer = np.array([], dtype=np.float32)
        processed_samples = 0
        last_text = ""
        # Preview ASR should feel immediate; keep this window shorter than the
        # final silence-closed transcript path so highlights can surface while
        # the caller is still speaking.
        emit_every_samples = TARGET_SAMPLE_RATE

        try:
            while True:
                try:
                    data = await websocket.receive()
                except RuntimeError as exc:
                    if "disconnect message has been received" in str(exc):
                        logger.info("Live websocket receive closed for case %s", case_id)
                        break
                    raise

                if "bytes" in data and data["bytes"] is not None:
                    if not active:
                        await websocket.send_json({"type": "error", "detail": "No active stream session"})
                        continue

                    chunk_count += 1
                    try:
                        if audio_encoding == "media_blob":
                            event = await asyncio.to_thread(
                                asr.transcribe_chunk,
                                data["bytes"],
                                chunk_count,
                                False,
                                mime_type,
                            )
                            text = event.text.strip()
                        else:
                            raw_audio = np.frombuffer(data["bytes"], dtype=np.float32)
                            if raw_audio.size == 0:
                                continue
                            raw_audio = _resample_audio(raw_audio, client_sample_rate, TARGET_SAMPLE_RATE)
                            audio_buffer = np.concatenate([audio_buffer, raw_audio])
                            max_preview_samples = TARGET_SAMPLE_RATE * MAX_PREVIEW_BUFFER_SECONDS
                            if len(audio_buffer) > max_preview_samples:
                                audio_buffer = audio_buffer[-max_preview_samples:]
                                processed_samples = min(processed_samples, len(audio_buffer))

                            if len(audio_buffer) - processed_samples < emit_every_samples:
                                continue

                            processed_samples = len(audio_buffer)
                            # Transcribe only the trailing N seconds
                            # of the rolling buffer to keep each ASR
                            # call bounded and predictable instead of
                            # re-transcribing the whole 12s window.
                            window_samples = (
                                TARGET_SAMPLE_RATE * PREVIEW_TRANSCRIBE_WINDOW_SECONDS
                            )
                            window = (
                                audio_buffer[-window_samples:]
                                if len(audio_buffer) > window_samples
                                else audio_buffer
                            )
                            text = await _transcribe_audio_buffer(asr, window, chunk_count)
                    except Exception as exc:
                        logger.exception("Live ASR failed for case %s", case_id)
                        await websocket.send_json(
                            {"type": "error", "detail": f"Live ASR failed: {exc}"}
                        )
                        continue

                    if text and text != last_text:
                        last_text = text
                        logger.info(
                            "Streaming transcript for case %s chunk %s: %d chars",
                            case_id,
                            chunk_count,
                            len(text),
                        )
                        await websocket.send_json(
                            {
                                "type": "transcript",
                                "case_id": case_id,
                                "speaker": "Caller",
                                "text": text,
                                "chunk_index": chunk_count,
                            }
                        )

                elif "text" in data and data["text"] is not None:
                    message = data["text"]
                    try:
                        payload = json.loads(message)
                    except Exception:
                        await websocket.send_json({"type": "error", "detail": "Invalid websocket payload"})
                        continue

                    msg_type = payload.get("type")
                    if msg_type == "start_stream":
                        active = True
                        audio_encoding = payload.get("encoding", "float32_pcm")
                        chunk_count = 0
                        audio_buffer = np.array([], dtype=np.float32)
                        processed_samples = 0
                        last_text = ""
                        await websocket.send_json(
                            {"type": "stream_started", "case_id": case_id, "user_id": user.id}
                        )
                    elif msg_type == "audio_config":
                        client_sample_rate = int(payload.get("sample_rate", 48000))
                        audio_encoding = payload.get("encoding", audio_encoding)
                        mime_type = payload.get("mime_type")
                        await websocket.send_json(
                            {
                                "type": "status",
                                "message": f"Audio configured at {client_sample_rate}Hz ({audio_encoding})",
                            }
                        )
                    elif msg_type in {"end_call", "stop_stream"}:
                        active = False
                        if audio_encoding != "media_blob" and audio_buffer.size > 0:
                            try:
                                text = await _transcribe_audio_buffer(asr, audio_buffer, chunk_count)
                            except Exception as exc:
                                logger.exception("Final live ASR failed for case %s", case_id)
                                await websocket.send_json(
                                    {"type": "error", "detail": f"Live ASR failed: {exc}"}
                                )
                            else:
                                if text and text != last_text:
                                    logger.info(
                                        "Final streaming transcript for case %s: %d chars",
                                        case_id,
                                        len(text),
                                    )
                                    await websocket.send_json(
                                        {
                                            "type": "transcript",
                                            "case_id": case_id,
                                            "speaker": "Caller",
                                            "text": text,
                                            "chunk_index": chunk_count,
                                        }
                                    )

                        await websocket.send_json(
                            {
                                "type": "stream_stopped",
                                "case_id": case_id,
                                "user_id": user.id,
                                "total_chunks": chunk_count,
                            }
                        )
                        await websocket.send_json({"type": "status", "message": "Call ended."})
                        break

        except WebSocketDisconnect:
            logger.info("Live websocket disconnected for case %s", case_id)
        finally:
            await manager.disconnect(case_id, websocket)
    finally:
        db.close()
