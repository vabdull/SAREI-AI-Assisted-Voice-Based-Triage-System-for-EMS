"""
Canonical live-state contract owned by the backend.

Every websocket/HTTP response that reports the state of a live case
MUST be shaped as ``CanonicalLivePayload`` (or a backwards-compatible
superset). This is the single contract the frontend consumes.

Why this exists
---------------
Before the refactor, four different payloads described "the current
state of a call": the /live-chunk response, the /live-analysis response,
the ``triage_update`` WS event and the ``triage_insight`` WS event.
They overlapped, had subtly different field names, and the frontend
picked a winner based on timing. That is the root cause of the
"fast result overwrites slow result overwrites fast result" flicker.

With ``CanonicalLivePayload``:

* there is exactly one shape that describes "where the case is right now";
* every result carries the ``transcript_revision`` it was computed against,
  so the backend can drop stale arrivals instead of them winning because
  they crossed the wire later;
* the backend is the ONLY place that computes ``display_triage``;
* legacy fields (``analysis``, ``patient_location``) are still emitted
  so the current UI keeps working while we transition.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.location import PatientLocation
from backend.app.schemas.triage_ai import AITriageAnalysis
from backend.app.triage_engine.models import (
    TriageFastResult,
    TriageMatch,
)


TranscriptStatus = Literal["empty", "preview", "current", "finalized"]
TriageLevel = Literal["red", "yellow", "green"]
TriageSource = Literal["fast", "enriched", "merged", "none"]
ResultKind = Literal["fast", "preview", "enriched", "snapshot"]


class DisplayTriage(BaseModel):
    """Backend-computed triage the UI should render.

    This is NEVER set by the frontend; it is a pure function of
    ``fast_triage`` and the latest non-stale ``enriched_triage`` under
    the merge rules in ``case_state_service``.
    """

    model_config = ConfigDict(extra="ignore")

    level: TriageLevel = "green"
    esi: int = Field(default=5, ge=1, le=5)
    esi_label_ar: str = "غير طارئ"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: TriageSource = "none"
    reasoning: list[str] = Field(default_factory=list)
    needs_confirmation: bool = True


class HighlightPayload(BaseModel):
    """A single transcript span the UI should highlight.

    Spans are ALWAYS grounded in the current transcript (substring of
    ``CaseLiveState.transcript_text``). The backend re-grounds on every
    revision bump so the UI never renders highlights that dangle into
    text the caller already corrected.
    """

    model_config = ConfigDict(extra="ignore")

    label: str
    canonical_label: str
    span_text: str
    start: int | None = None
    end: int | None = None
    severity: Literal["high", "medium", "low"] = "medium"
    negated: bool = False
    uncertain: bool = False
    current: bool = True
    source: Literal["fast", "enriched"] = "fast"


class CaseLiveState(BaseModel):
    """Authoritative in-memory live state for a case.

    Held by ``case_state_service``. Serialised as
    ``CanonicalLivePayload`` for transport.
    """

    model_config = ConfigDict(extra="ignore")

    case_id: int

    transcript_revision: int = 0
    transcript_text: str = ""
    transcript_status: TranscriptStatus = "empty"
    preview_transcript_text: str = ""

    fast_triage: Optional[TriageFastResult] = None
    fast_triage_revision: int = 0

    enriched_triage: Optional[AITriageAnalysis] = None
    enriched_triage_revision: int = 0

    display_triage: DisplayTriage = Field(default_factory=DisplayTriage)

    highlights: list[HighlightPayload] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    location: Optional[PatientLocation] = None
    location_revision: int = 0

    patient_count: Optional[int] = None

    # Deterministically extracted patient demographics from the live
    # transcript (best-effort; None until spoken/detected).
    patient_name: Optional[str] = None
    patient_age: Optional[int] = None
    patient_gender: Optional[str] = None

    reasoning: list[str] = Field(default_factory=list)

    chunk_count: int = 0
    last_source_ts_ms: float = 0.0
    last_fast_ts_ms: float = 0.0
    last_enriched_ts_ms: float = 0.0

    provisional: bool = False


class CanonicalLivePayload(BaseModel):
    """Exactly what crosses the wire to the frontend.

    This is the ``type="live_state"`` WS payload and the ``/live-chunk``
    HTTP response body. It embeds the full ``CaseLiveState`` plus
    backwards-compatible legacy fields the current UI still reads.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["live_state"] = "live_state"

    state: CaseLiveState
    fast_matches: list[TriageMatch] = Field(default_factory=list)

    # ── Legacy compatibility fields (do not add new callers) ─────────
    # The existing dispatcher UI reads these exact fields; keeping them
    # wired means the UI works unchanged while the backend transitions.
    analysis: AITriageAnalysis = Field(default_factory=AITriageAnalysis)
    patient_location: Optional[PatientLocation] = None
    extraction_confidence: Optional[float] = None
    live_transcript_text: Optional[str] = None
