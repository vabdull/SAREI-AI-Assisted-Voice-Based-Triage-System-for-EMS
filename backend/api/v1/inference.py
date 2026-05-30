"""HTTP routes for the live ASR pipeline.

These handlers are intentionally thin: they parse/validate the request,
run ASR, and delegate the rest to the services that own the logic:

* ``transcript_service`` — merge + revision bump + DB persistence,
* ``fast_decision_service`` — deterministic fast triage / highlights /
  location / patient count,
* ``case_state_service`` — authoritative merge + broadcast + legacy
  DB mirror + enrichment scheduling.

Business logic belongs in those services, not in this module.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
from dataclasses import asdict

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

import soundfile as sf

from backend.ai.asr_runtime import BatchTranscriptResult, StreamingAsrService
from backend.ai.audio_preprocessing import preprocess_audio_chunk
from backend.ai.triage_analysis_service import AITriageAnalysisService
from backend.api.deps import get_current_user
from backend.api.v1.dispatcher import save_upload_file_to_secure_recording
from backend.core.paths import resolve_existing_storage_path
from backend.db.models import Case, User
from backend.db.session import get_db
from backend.schemas.triage_ai import HighlightItem
from backend.schemas.inference import (
    AnalyzeTextRequest,
    AnalyzeTextResponse,
    BatchTranscriptionItem,
    BatchTranscriptionResponse,
    FinalizeRecordingResponse,
    LiveAnalysisResponse,
    LiveChunkResponse,
    LiveWarmupResponse,
)
from backend.services.case_state_service import get_case_state_service
from backend.services.fast_decision_service import get_fast_decision_service
from backend.services.media_service import save_bytes_to_secure_recording
from backend.services.observability import log_stage, stage_timer
from backend.services.recording_buffer_service import (
    get_recording_buffer_service,
)
from backend.services.transcript_service import (
    get_transcript_service,
    merge_transcript_text,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/live-chunk", response_model=LiveChunkResponse)
def transcribe_live_chunk(
    case_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LiveChunkResponse:
    """The single live ASR entrypoint.

    Flow (critical path):

    1. read + preprocess audio,
    2. ASR,
    3. ``transcript_service`` merge + revision bump,
    4. ``fast_decision_service`` — deterministic triage/highlights,
    5. ``case_state_service.apply_fast`` — merge + broadcast +
       schedule enrichment.

    The response shape is preserved for the existing UI.
    """
    critical_start = time.perf_counter()

    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    with stage_timer(None, "received", case_id=case_id):
        raw = file.file.read()
        mime_type = file.content_type or "audio/webm"
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    logger.info(
        "Live chunk received: case=%s mime=%s bytes=%d filename=%s",
        case_id,
        mime_type,
        len(raw),
        file.filename or "unknown",
    )

    transcript_service = get_transcript_service()
    fast_service = get_fast_decision_service()
    state_service = get_case_state_service()

    # ── 1/2: preprocess + ASR (hot path) ─────────────────────────
    try:
        with stage_timer(None, "preprocess", case_id=case_id):
            processed_bytes, metadata = preprocess_audio_chunk(
                raw,
                mime_type=mime_type,
                enable_noise_reduction=False,
            )
    except Exception as exc:
        logger.warning(
            "Live chunk preprocessing failed case=%s mime=%s bytes=%d: %s",
            case_id,
            mime_type,
            len(raw),
            exc,
        )
        # Never fail the live session on a corrupt chunk — return the
        # last-known canonical state so the UI keeps rendering.
        state = state_service.get(case_id)
        return _legacy_live_chunk_response(state, metadata_dict={})

    metadata_dict = asdict(metadata) if metadata else {}

    # Buffer the decoded PCM so the call can be saved as a single
    # recording on finalize. Best-effort — never block the live path.
    try:
        get_recording_buffer_service().append_wav_bytes(case_id, processed_bytes)
    except Exception:
        logger.warning("recording buffer append failed case=%s", case_id, exc_info=True)

    try:
        chunk_text = _run_asr(processed_bytes, case_id=case_id)
    except Exception as exc:
        logger.exception("Live ASR failed for case %s", case_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ASR failed: {exc}",
        ) from exc

    # ── 3: transcript merge + revision ───────────────────────────
    merge_result = transcript_service.ingest_chunk(
        db=db, case_id=case_id, chunk_text=chunk_text
    )

    # ── 4: fast decisions (deterministic, no LLM) ────────────────
    state = state_service.get(case_id)
    state_service.apply_transcript_update(
        case_id=case_id,
        transcript_text=merge_result.text,
        revision=merge_result.revision,
    )

    if chunk_text.strip():
        fast = fast_service.process(
            case_id=case_id,
            revision=merge_result.revision,
            chunk_text=chunk_text,
            transcript_text=merge_result.text,
            provisional=False,
        )
        # ── 5: merge + broadcast + schedule enrichment ───────────
        state_service.apply_fast(case_id=case_id, fast=fast, provisional=False)

    total_ms = (time.perf_counter() - critical_start) * 1000.0
    log_stage(
        stage="total_critical",
        latency_ms=total_ms,
        case_id=case_id,
        revision=merge_result.revision,
        result_kind="fast",
        chunk_len=len(chunk_text),
    )

    state = state_service.get(case_id)
    return _legacy_live_chunk_response(
        state, metadata_dict=metadata_dict, chunk_text=chunk_text
    )


@router.post("/live-analysis", response_model=LiveAnalysisResponse)
def get_live_analysis(
    case_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LiveAnalysisResponse:
    """Read-only view of the canonical live state.

    This endpoint used to run the LLM synchronously on every poll.
    After the refactor it returns the authoritative ``CaseLiveState``
    and does NOT trigger an LLM call. Enrichment is event-driven from
    ``/live-chunk``; if no chunk has arrived yet there is no live
    state to enrich.
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    state_service = get_case_state_service()
    state = state_service.get(case_id)

    analysis = state_service._compose_legacy_analysis(state)
    # ``analyzed_revision`` is the revision the enriched analysis was
    # produced for. If no enriched run has completed yet we fall back to
    # the fast revision (which still produced grounded keywords /
    # location) so callers can distinguish "no data" from "stale data".
    analyzed_revision = (
        state.enriched_triage_revision
        if state.enriched_triage_revision > 0
        else state.fast_triage_revision
    )
    return LiveAnalysisResponse(
        analysis=analysis,
        live_transcript_text=state.transcript_text or None,
        analyzed_transcript_text=(
            state.transcript_text
            if state.enriched_triage_revision == state.transcript_revision
            else None
        ),
        analysis_in_progress=state.enriched_triage_revision < state.transcript_revision,
        transcript_revision=state.transcript_revision,
        analyzed_revision=analyzed_revision,
    )


@router.post("/live-warmup", response_model=LiveWarmupResponse)
def warm_live_analysis(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
) -> LiveWarmupResponse:
    """Warm the realtime LLM in the background so the first call is fast."""
    background_tasks.add_task(AITriageAnalysisService().warm_realtime_models)
    return LiveWarmupResponse(queued=True)


@router.post("/finalize-recording", response_model=FinalizeRecordingResponse)
def finalize_recording(
    case_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FinalizeRecordingResponse:
    """Save the live call's buffered audio as a single recording.

    Called by the dispatcher UI when a call ends. Concatenates the
    decoded 16 kHz PCM chunks captured during ``/live-chunk`` into one
    WAV file and creates a ``CallRecording`` row. Idempotent-ish: once
    the buffer is drained a second call returns ``saved=False``.
    """
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    buffer = get_recording_buffer_service()
    wav_bytes = buffer.finalize_to_wav(case_id)
    if not wav_bytes:
        return FinalizeRecordingResponse(saved=False)

    original_filename = f"live-call-{case_id}.wav"
    try:
        recording = save_bytes_to_secure_recording(
            db,
            case_id=case_id,
            user_id=current_user.id,
            audio_bytes=wav_bytes,
            original_filename=original_filename,
        )
    except Exception as exc:
        logger.exception("Failed to save live recording for case=%s", case_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save recording: {exc}",
        ) from exc

    # WAV (PCM_16, 16 kHz mono): 2 bytes/sample, minus a 44-byte header.
    duration = max(0.0, (len(wav_bytes) - 44) / (2 * 16000))
    logger.info(
        "Saved live recording id=%s case=%s bytes=%d duration=%.1fs",
        recording.id,
        case_id,
        len(wav_bytes),
        duration,
    )
    return FinalizeRecordingResponse(
        saved=True,
        recording_id=recording.id,
        file_size_bytes=len(wav_bytes),
        duration_seconds=round(duration, 1),
    )


@router.post("/analyze-text", response_model=AnalyzeTextResponse)
def analyze_text(
    payload: AnalyzeTextRequest,
    current_user: User = Depends(get_current_user),
) -> AnalyzeTextResponse:
    """Run the full AI triage analysis on free-typed text.

    Powers the Manual Case Entry "Generate AI Suggestion" button: the
    dispatcher types symptoms / a description, and the same LLM triage
    engine used for live calls returns a triage level + confidence +
    Arabic reasoning + grounded highlights + mechanism-of-injury, which
    the form maps to a suggested severity / emergency type / keywords.
    """
    text = (payload.text or "").strip()
    if len(text) < 3:
        return AnalyzeTextResponse()
    try:
        analysis = AITriageAnalysisService().analyze_transcript(text)
    except Exception as exc:  # surface a clean 502 instead of a 500 trace
        logger.warning("analyze-text failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI analysis is unavailable right now. Please try again.",
        ) from exc

    # Critical-keyword highlights come from the deterministic matcher —
    # the same engine the live UI uses and the single source of highlight
    # truth in this system. It's more reliable than the LLM for spotting
    # keywords, so prefer it; only keep the LLM's highlights when the
    # matcher finds nothing.
    try:
        # ``provisional`` = a one-shot "what-if" evaluation that does not
        # mutate any live case state. ``case_id=-1`` is a sentinel that
        # never collides with a real case (ids start at 1).
        fast = get_fast_decision_service().process(
            case_id=-1,
            revision=0,
            chunk_text=text,
            transcript_text=text,
            provisional=True,
        )
        fast_highlights = [
            HighlightItem(
                label=h.label,
                canonical_label=h.canonical_label,
                span_text=h.span_text,
                start=h.start,
                end=h.end,
                severity=h.severity,
                negated=h.negated,
                uncertain=h.uncertain,
                current=h.current,
            )
            for h in fast.highlights
        ]
        if fast_highlights:
            analysis.highlights = fast_highlights
    except Exception:  # highlights are best-effort; never fail the request
        logger.warning(
            "fast highlight grounding for analyze-text failed", exc_info=True
        )

    return AnalyzeTextResponse(analysis=analysis)


@router.post("/batch-transcribe", response_model=BatchTranscriptionResponse)
def batch_transcribe(
    case_id: int = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BatchTranscriptionResponse:
    """Transcribe uploaded recordings for a case and merge into its transcript."""
    logger.info(
        "Batch transcription request: case=%s files=%d",
        case_id,
        len(files),
    )
    case = db.query(Case).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Case not found",
        )

    recordings = []
    for upload in files:
        recording = save_upload_file_to_secure_recording(db, case, upload, current_user)
        recordings.append(recording)

    audio_paths: list[str] = []
    for rec in recordings:
        resolved = resolve_existing_storage_path(rec.storage_path)
        if not resolved.is_file():
            logger.error(
                "Recording %s storage path could not be resolved (raw=%r)",
                rec.id,
                rec.storage_path,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Recording {rec.id} file not found on disk",
            )
        audio_paths.append(str(resolved))

    try:
        asr = StreamingAsrService()
        transcription_results: list[BatchTranscriptResult] = asr.transcribe_files(
            audio_paths
        )
    except Exception as exc:
        logger.exception("Batch ASR failed case=%s", case_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ASR transcription failed: {exc}",
        ) from exc

    transcript_service = get_transcript_service()
    live_text = transcript_service.text(case_id)
    items: list[BatchTranscriptionItem] = []
    for rec, result in zip(recordings, transcription_results):
        normalized_text = result.text.strip()
        if normalized_text:
            live_text = merge_transcript_text(live_text, normalized_text)
        items.append(
            BatchTranscriptionItem(
                audio_path=result.audio_path,
                text=normalized_text,
                preprocessing=asdict(result.preprocessing) if result.preprocessing else {},
                recording_id=rec.id,
                audio_label=rec.original_filename,
            )
        )

    if live_text and live_text != transcript_service.text(case_id):
        transcript_service.ingest_chunk(db=db, case_id=case_id, chunk_text=live_text)

    return BatchTranscriptionResponse(
        results=items,
        live_transcript_text=live_text or None,
    )


# ── helpers ──────────────────────────────────────────────────────────


def _run_asr(processed_bytes: bytes, *, case_id: int) -> str:
    """Call the existing NeMo ASR on a single preprocessed chunk.

    Kept here (not in transcript_service) because transcript_service
    must stay decoupled from the ASR technology. If NeMo is ever
    replaced this is the only function that changes.
    """
    asr = StreamingAsrService()
    with stage_timer(None, "asr", case_id=case_id):
        audio_data, sr = sf.read(io.BytesIO(processed_bytes), dtype="float32")
        if audio_data.ndim > 1:
            audio_data = audio_data.mean(axis=1)
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name
            sf.write(tmp_path, audio_data, sr)
            chunk_text = asr._transcribe_paths([tmp_path], batch_size=1)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return (chunk_text[0] if chunk_text else "").strip()


def _legacy_live_chunk_response(
    state,
    *,
    metadata_dict: dict,
    chunk_text: str = "",
) -> LiveChunkResponse:
    """Build the ``/live-chunk`` legacy response from canonical state.

    The dispatcher UI reads ``analysis``, ``patient_location``,
    ``extraction_confidence``, and ``live_transcript_text``; we feed
    all of them from the single source of truth.
    """
    state_service = get_case_state_service()
    analysis = state_service._compose_legacy_analysis(state)
    return LiveChunkResponse(
        text=chunk_text,
        live_transcript_text=state.transcript_text or None,
        patient_location=state.location,
        extraction_confidence=state.display_triage.confidence or None,
        analysis=analysis,
        preprocessing=metadata_dict,
        transcript_revision=state.transcript_revision,
    )
