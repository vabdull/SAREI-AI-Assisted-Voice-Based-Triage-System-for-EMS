"""Request/response models for the inference (ASR + analysis) API.

Defines the contracts for batch transcription, the live audio-chunk
endpoint, and live-analysis polling. Several responses carry a
``transcript_revision`` so clients can detect and discard stale results.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from backend.schemas.location import PatientLocation
from backend.schemas.triage_ai import AITriageAnalysis


class BatchTranscriptionRequest(BaseModel):
    """List of stored audio file paths to transcribe in one request."""

    audio_paths: list[str]


class BatchTranscriptionItem(BaseModel):
    """Transcription result for one audio file in a batch."""

    audio_path: str
    text: str
    preprocessing: dict = {}
    recording_id: Optional[int] = None
    audio_label: Optional[str] = None


class BatchTranscriptionResponse(BaseModel):
    """Per-file results plus the merged live transcript, if any."""

    results: list[BatchTranscriptionItem]
    live_transcript_text: Optional[str] = None


class LiveChunkResponse(BaseModel):
    text: str
    live_transcript_text: Optional[str] = None
    patient_location: Optional[PatientLocation] = None
    extraction_confidence: Optional[float] = None
    analysis: AITriageAnalysis = AITriageAnalysis()
    preprocessing: dict = {}
    # transcript revision after this chunk was merged. Callers can use
    # it as an etag / staleness guard when polling.
    transcript_revision: int = 0


class LiveAnalysisResponse(BaseModel):
    analysis: AITriageAnalysis = AITriageAnalysis()
    live_transcript_text: Optional[str] = None
    analyzed_transcript_text: Optional[str] = None
    analysis_in_progress: bool = False
    # Current authoritative transcript revision (monotonic per case).
    transcript_revision: int = 0
    # Revision the embedded ``analysis`` was produced for. If this is
    # strictly less than the caller's last-seen revision, the caller MUST
    # discard ``analysis``: it predates newer state already shown via the
    # WebSocket and would otherwise overwrite it.
    analyzed_revision: int = 0


class LiveWarmupResponse(BaseModel):
    queued: bool = True


class AnalyzeTextRequest(BaseModel):
    """Free-text triage analysis (Manual Case Entry 'AI Suggestion')."""

    text: str


class AnalyzeTextResponse(BaseModel):
    analysis: AITriageAnalysis = AITriageAnalysis()


class FinalizeRecordingResponse(BaseModel):
    """Result of saving a live call's combined audio as one recording."""

    saved: bool = False
    recording_id: Optional[int] = None
    file_size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None
