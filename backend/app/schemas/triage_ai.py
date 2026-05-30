"""Schemas describing the structured triage analysis produced by the LLM.

These models define the wire format for symptoms, injuries, highlighted
phrases, patient state, and the final triage assessment. Validators keep
the LLM's output well-formed (e.g. clamping confidence into [0, 1]).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from backend.app.schemas.location import PatientLocation


TriageLevel = Literal["red", "yellow", "green"]
SeverityLevel = Literal["high", "medium", "low"]


class HighlightItem(BaseModel):
    """A transcript phrase the model flagged, with span offsets and flags."""

    label: str
    canonical_label: str
    span_text: str
    start: int | None = None
    end: int | None = None
    severity: SeverityLevel = "medium"
    negated: bool = False
    uncertain: bool = False
    current: bool = True


class MedicalEntityItem(BaseModel):
    canonical_label: str
    spoken_text: str
    severity: SeverityLevel = "medium"
    negated: bool = False
    uncertain: bool = False
    current: bool = True
    speaker: str = "patient"


class PatientState(BaseModel):
    consciousness: str = "unknown"
    breathing: str = "unknown"
    bleeding: str = "unknown"


class MedicalEntities(BaseModel):
    symptoms: list[MedicalEntityItem] = Field(default_factory=list)
    injuries: list[MedicalEntityItem] = Field(default_factory=list)
    patient_state: PatientState = Field(default_factory=PatientState)
    risk_factors: list[str] = Field(default_factory=list)
    mechanism_of_injury: list[str] = Field(default_factory=list)
    resolved_clues: list[str] = Field(default_factory=list)
    timeline_clues: list[str] = Field(default_factory=list)


class TriageAssessment(BaseModel):
    """The model's triage level plus a bounded confidence and reasoning."""

    level: TriageLevel = "green"
    # Confidence must stay within [0, 1]. The validator below normalises
    # any malformed value the LLM may emit (a numeric string, a float
    # above 1, or NaN) so the stored value and the dispatcher's confidence
    # display always represent a valid percentage.
    confidence: float = 0.0
    reasoning: list[str] = Field(default_factory=list)
    needs_confirmation: bool = True

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        # NaN check
        if f != f:
            return 0.0
        if f < 0.0:
            return 0.0
        if f > 1.0:
            return 1.0
        return f


class AnalysisMeta(BaseModel):
    engine_version: str = "ai_v2"
    language: str = "ar"
    dialect_handling: bool = True


class AITriageAnalysis(BaseModel):
    highlights: list[HighlightItem] = Field(default_factory=list)
    medical_entities: MedicalEntities = Field(default_factory=MedicalEntities)
    triage: TriageAssessment = Field(default_factory=TriageAssessment)
    patient_location: Optional[PatientLocation] = None
    meta: AnalysisMeta = Field(default_factory=AnalysisMeta)
