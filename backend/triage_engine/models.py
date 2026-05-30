"""Pydantic models for the triage engine's fast path (WS payloads + state)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.schemas.triage_ai import AITriageAnalysis


TriageLevel = Literal["red", "yellow", "green"]
Dialect = Literal["msa", "najdi", "hijazi", "khaleeji", "universal", "unknown"]


class TriageEvidenceSpan(BaseModel):
    """A matched text span as character offsets plus the verbatim substring.

    The matcher emits offsets into the normalized transcript; mapping
    them back to the original (pre-normalization) text happens downstream.
    """

    model_config = ConfigDict(extra="ignore")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(description="Verbatim substring from the source text.")


class TriageMatch(BaseModel):
    """A single symptom / concept match emitted by the fuzzy matcher."""

    model_config = ConfigDict(extra="ignore")

    concept_id: str
    category: str
    esi: int = Field(ge=1, le=5)
    weight: int = Field(ge=0, le=10)
    canonical_label_ar: str
    matched_keyword: str
    matched_dialect: Dialect = "unknown"
    fuzzy_score: float = Field(ge=0.0, le=100.0)
    is_fuzzy: bool = True
    negated: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    spans: list[TriageEvidenceSpan] = Field(default_factory=list)
    last_seen_at: float = 0.0  # perf_counter seconds when last observed


class TriageRiskModifier(BaseModel):
    model_config = ConfigDict(extra="ignore")

    modifier_id: str
    note_ar: str
    escalate: bool
    trigger: str
    spans: list[TriageEvidenceSpan] = Field(default_factory=list)


class TriageFastResult(BaseModel):
    """Fast-path (Layer 1 + Layer 2) output — emitted within ~15ms of each bubble."""

    model_config = ConfigDict(extra="ignore")

    esi: int = Field(ge=1, le=5)
    esi_label_ar: str
    level: TriageLevel
    escalated: bool = False
    matches: list[TriageMatch] = Field(default_factory=list)
    modifiers: list[TriageRiskModifier] = Field(default_factory=list)
    processing_time_ms: float = 0.0


class TriageFastEvent(BaseModel):
    """`type="triage_update"` WS payload. Purely fast path — no LLM."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["triage_update"] = "triage_update"
    case_id: int
    chunk_index: int
    chunk_text: str
    full_transcript: str
    result: TriageFastResult
    # Matches discovered in this chunk specifically (vs. accumulated state).
    chunk_matches: list[TriageMatch] = Field(default_factory=list)
    provisional: bool = False
    client_sent_at_ms: Optional[float] = None


class TriageInsightEvent(BaseModel):
    """`type="triage_insight"` WS payload. LLM-enriched, fires on silence."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["triage_insight"] = "triage_insight"
    case_id: int
    full_transcript: str
    analysis: AITriageAnalysis
    # Fast path at the time the LLM was scheduled; lets the client compare.
    fast_result: Optional[TriageFastResult] = None
    llm_latency_ms: Optional[float] = None
    timed_out: bool = False


class TriageResetEvent(BaseModel):
    """`type="triage_reset"`. Sent when a new call starts for a case."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["triage_reset"] = "triage_reset"
    case_id: int


ESI_LABELS_AR: dict[int, str] = {
    1: "حرج - فوري",
    2: "طارئ",
    3: "عاجل",
    4: "اقل الحاحا",
    5: "غير طارئ",
}
