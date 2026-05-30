"""Secure storage and retrieval of call-recording audio files.

Validates uploaded audio (size limit plus magic-byte / extension type
check), writes it to disk under a randomly generated filename to avoid
path-traversal and collisions, records a ``CallRecording`` row, and
serves files back as download responses.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets

from fastapi import HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.db.models import CallRecording, CallRecordingStatus

logger = logging.getLogger(__name__)

# Accepted audio MIME types for uploaded recordings.
ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/webm",
    "audio/flac",
}

# Leading bytes that identify common audio containers, used to verify the
# real format rather than trusting the client-supplied extension/MIME.
MAGIC_HEADERS: dict[bytes, str] = {
    b"RIFF": "audio/wav",
    b"ID3": "audio/mpeg",
    b"\xff\xfb": "audio/mpeg",
    b"OggS": "audio/ogg",
    b"fLaC": "audio/flac",
}


def validate_audio_content(data: bytes, filename: str) -> str:
    """Validate size and audio type; return the resolved MIME type.

    Prefers detection by magic bytes and falls back to the file
    extension. Raises an HTTP error for oversized, too-small, or
    unsupported files.
    """
    settings = get_settings()
    max_bytes = settings.max_recording_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum size of {settings.max_recording_size_mb} MB",
        )
    if len(data) < 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is too small to be a valid audio file",
        )

    detected_type: str | None = None
    for magic, mime in MAGIC_HEADERS.items():
        if data[: len(magic)] == magic:
            detected_type = mime
            break

    if detected_type and detected_type in ALLOWED_AUDIO_TYPES:
        return detected_type

    ext = os.path.splitext(filename)[1].lower()
    ext_map = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
    }
    fallback = ext_map.get(ext)
    if fallback and fallback in ALLOWED_AUDIO_TYPES:
        return fallback

    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail=f"Unsupported audio format for file '{filename}'",
    )


def save_bytes_to_secure_recording(
    db: Session,
    case_id: int,
    user_id: int,
    audio_bytes: bytes,
    original_filename: str,
) -> CallRecording:
    """Validate and store raw audio bytes, returning the new DB record.

    The file is saved under a random ``secrets``-generated name to avoid
    using untrusted client filenames on disk; the original name is kept
    only as metadata. A SHA-256 of the content is logged for traceability.
    """
    mime_type = validate_audio_content(audio_bytes, original_filename)

    settings = get_settings()
    storage_dir = settings.recording_storage_dir
    os.makedirs(storage_dir, exist_ok=True)

    ext = os.path.splitext(original_filename)[1].lower() or ".bin"
    secure_name = f"{secrets.token_hex(16)}{ext}"
    file_path = os.path.join(storage_dir, secure_name)

    sha256_hex = hashlib.sha256(audio_bytes).hexdigest()
    with open(file_path, "wb") as f:
        f.write(audio_bytes)

    recording = CallRecording(
        case_id=case_id,
        uploaded_by_id=user_id,
        original_filename=original_filename,
        secure_filename=secure_name,
        storage_path=file_path,
        file_size_bytes=len(audio_bytes),
        mime_type=mime_type,
        status=CallRecordingStatus.ready,
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)

    logger.info(
        "Saved recording %s (%d bytes, sha256=%s) for case %s",
        secure_name,
        len(audio_bytes),
        sha256_hex[:12],
        case_id,
    )
    return recording


async def save_upload_file_to_secure_recording(
    db: Session,
    case_id: int,
    user_id: int,
    upload_file: UploadFile,
) -> CallRecording:
    """Read an ``UploadFile`` and store it as a secure recording."""
    audio_bytes = await upload_file.read()
    original_filename = upload_file.filename or "upload.bin"
    return save_bytes_to_secure_recording(
        db, case_id, user_id, audio_bytes, original_filename,
    )


def get_recording_or_404(db: Session, recording_id: int) -> CallRecording:
    """Return the recording row by id, or raise 404 if it does not exist."""
    recording = (
        db.query(CallRecording)
        .filter(CallRecording.id == recording_id)
        .first()
    )
    if recording is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recording {recording_id} not found",
        )
    return recording


def recording_file_response(recording: CallRecording) -> FileResponse:
    """Build a download response for a recording; 404 if the file is missing."""
    if not os.path.isfile(recording.storage_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recording file not found on disk",
        )
    return FileResponse(
        path=recording.storage_path,
        media_type=recording.mime_type,
        filename=recording.original_filename,
    )
