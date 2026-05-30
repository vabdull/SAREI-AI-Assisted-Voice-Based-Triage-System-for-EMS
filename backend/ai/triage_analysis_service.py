"""LLM-backed triage analysis (the "enriched" layer).

Sends the running transcript to an Ollama/Qwen model and parses the
structured response into the ``AITriageAnalysis`` schema: triage level,
confidence, highlighted phrases, medical entities, and location. Provides
both a full analysis path and a lighter realtime (triage-only) path.

Note: ``find_phrase_token_aware`` is imported lazily inside
``_sanitize_highlight_items`` to avoid a circular import (importing the
triage_engine package at module load would loop back through
``llm_enricher`` into this module). The lazy import is cached and its
cost is negligible next to an LLM round-trip.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from typing import Any, Literal, Sequence
from urllib import error, request
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field

from backend.core.config import get_settings
from backend.schemas.location import (
    LocationComponents,
    LocationGeocode,
    LocationSourceSpan,
    PatientLocation,
)
from backend.schemas.triage_ai import (
    AITriageAnalysis,
    AnalysisMeta,
    HighlightItem,
    MedicalEntities,
    MedicalEntityItem,
    TriageAssessment,
)

_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF]")


def _has_arabic(text: str | None) -> bool:
    return bool(text and _ARABIC_CHAR_RE.search(text))


def _phrase_has_transcript_support(phrase: str, transcript: str) -> bool:
    """
    Return True if `phrase` is plausibly grounded in `transcript`.

    This is a pure substring/word check — not a medical rule. A phrase is
    accepted if it appears verbatim, or if any of its meaningful words
    (length >= 3 with Arabic characters) appears verbatim. This stops the
    LLM from inventing concepts that have no textual support at all, while
    still allowing it to paraphrase the canonical label of something that
    was said.
    """
    if not phrase:
        return False
    phrase = phrase.strip()
    if not phrase:
        return False
    if phrase in transcript:
        return True
    for word in phrase.split():
        word = word.strip()
        if len(word) < 3 or not _has_arabic(word):
            continue
        if word in transcript:
            return True
    return False


def _sanitize_entity_item(
    item: MedicalEntityItem, transcript: str
) -> MedicalEntityItem | None:
    spoken = _normalize_string(item.spoken_text)
    if not spoken or spoken not in transcript:
        return None
    canonical = _normalize_string(item.canonical_label)
    if not canonical or not _has_arabic(canonical):
        canonical = spoken
    return item.model_copy(
        update={"canonical_label": canonical, "spoken_text": spoken}
    )


def _sanitize_medical_entities(
    entities: MedicalEntities, transcript: str
) -> MedicalEntities:
    symptoms: list[MedicalEntityItem] = []
    for raw in entities.symptoms:
        cleaned = _sanitize_entity_item(raw, transcript)
        if cleaned is not None:
            symptoms.append(cleaned)

    injuries: list[MedicalEntityItem] = []
    for raw in entities.injuries:
        cleaned = _sanitize_entity_item(raw, transcript)
        if cleaned is not None:
            injuries.append(cleaned)

    mechanism_of_injury = [
        m.strip()
        for m in entities.mechanism_of_injury
        if _has_arabic(m) and _phrase_has_transcript_support(m, transcript)
    ]
    risk_factors = [
        r.strip()
        for r in entities.risk_factors
        if _has_arabic(r) and _phrase_has_transcript_support(r, transcript)
    ]
    resolved_clues = [
        c.strip()
        for c in entities.resolved_clues
        if _has_arabic(c) and _phrase_has_transcript_support(c, transcript)
    ]
    timeline_clues = [
        c.strip()
        for c in entities.timeline_clues
        if _has_arabic(c) and _phrase_has_transcript_support(c, transcript)
    ]

    return entities.model_copy(
        update={
            "symptoms": symptoms,
            "injuries": injuries,
            "mechanism_of_injury": mechanism_of_injury,
            "risk_factors": risk_factors,
            "resolved_clues": resolved_clues,
            "timeline_clues": timeline_clues,
        }
    )


def _sanitize_triage_assessment(triage: TriageAssessment) -> TriageAssessment:
    reasoning = [
        r.strip() for r in triage.reasoning if r and _has_arabic(r)
    ]
    return triage.model_copy(update={"reasoning": reasoning})


def _sanitize_highlight_items(
    transcript: str, highlights: list[HighlightItem]
) -> list[HighlightItem]:
    """Drop LLM highlights whose ``span_text`` doesn't token-align with
    the live transcript.

    The pre-2026-05 implementation used ``span not in transcript`` which
    is a plain substring check — if the LLM hallucinated a short span
    like ``سم`` and the transcript contained the unrelated word
    ``اسمي``, the substring check passed and the bad span got painted.
    We now require the same Arabic-token boundary alignment used by the
    fast path. If the LLM returns a verbatim transcript slice it still
    passes; only partial-token spans are filtered out.
    """
    # Lazy import — see module-level NOTE about the circular import.
    from backend.triage_engine.normalization import (
        find_phrase_token_aware,
    )

    kept: list[HighlightItem] = []
    for item in highlights:
        span = _normalize_string(item.span_text)
        if not span:
            continue
        if find_phrase_token_aware(
            transcript,
            span,
            occurrence="last",
            allow_clitic_prefix=True,
        ) is None:
            continue
        label = item.label if _has_arabic(item.label) else span
        canonical = (
            item.canonical_label
            if _has_arabic(item.canonical_label)
            else span
        )
        kept.append(
            item.model_copy(
                update={
                    "label": label,
                    "canonical_label": canonical,
                    "span_text": span,
                }
            )
        )
    return kept

logger = logging.getLogger(__name__)
_LAST_GOOD_OLLAMA_BASE_URL: str | None = None
_LAST_GOOD_OLLAMA_MODEL: str | None = None
_LAST_WARMED_AT: dict[tuple[str, str], float] = {}
_WARMUP_COOLDOWN_SECONDS = 300


def _empty_analysis() -> AITriageAnalysis:
    return AITriageAnalysis()


def _normalize_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _running_in_wsl() -> bool:
    return Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()


def _wsl_windows_gateway_ip() -> str | None:
    route_path = Path("/proc/net/route")
    if not route_path.exists():
        return None
    try:
        for line in route_path.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 3:
                continue
            destination = parts[1]
            gateway_hex = parts[2]
            if destination != "00000000":
                continue
            octets = [str(int(gateway_hex[index:index + 2], 16)) for index in range(0, 8, 2)]
            octets.reverse()
            return ".".join(octets)
    except OSError:
        return None
    return None


def _find_span(
    transcript_text: str,
    span_text: str,
    occupied_ranges: list[tuple[int, int]],
    preferred_start: int | None = None,
) -> tuple[int | None, int | None]:
    candidates: list[tuple[int, int]] = []
    start_at = 0
    while True:
        idx = transcript_text.find(span_text, start_at)
        if idx == -1:
            break
        candidates.append((idx, idx + len(span_text)))
        start_at = idx + 1

    if not candidates:
        return None, None

    def is_available(candidate: tuple[int, int]) -> bool:
        return all(candidate[1] <= start or candidate[0] >= end for start, end in occupied_ranges)

    available = [candidate for candidate in candidates if is_available(candidate)]
    if not available:
        available = candidates

    if preferred_start is not None:
        available.sort(key=lambda item: abs(item[0] - preferred_start))

    chosen = available[0]
    occupied_ranges.append(chosen)
    return chosen


def _align_highlights(
    transcript_text: str,
    highlights: list[HighlightItem],
) -> list[HighlightItem]:
    aligned: list[HighlightItem] = []
    occupied_ranges: list[tuple[int, int]] = []
    for item in highlights:
        span_text = _normalize_string(item.span_text)
        if not span_text:
            continue
        if (
            item.start is not None
            and item.end is not None
            and 0 <= item.start < item.end <= len(transcript_text)
            and transcript_text[item.start:item.end] == span_text
        ):
            occupied_ranges.append((item.start, item.end))
            aligned.append(item)
            continue
        start, end = _find_span(transcript_text, span_text, occupied_ranges, preferred_start=item.start)
        if start is None or end is None:
            continue
        aligned.append(item.model_copy(update={"start": start, "end": end, "span_text": span_text}))
    return aligned


def _sanitize_patient_location(
    transcript_text: str, location: PatientLocation | None
) -> PatientLocation | None:
    """Validate a LLM-produced PatientLocation against the live transcript.

    - If the LLM claimed a span, re-anchor it by searching the raw_text in
      the transcript. If not found, drop the span (but keep the location).
    - If raw_text is empty, drop the whole location (None).
    - Preserves components and geocode untouched.
    """
    if location is None:
        return None
    raw_text = _normalize_string(location.raw_text)
    if not raw_text:
        return None

    span: LocationSourceSpan | None = None
    if location.source_span is not None:
        s = location.source_span.start
        e = location.source_span.end
        if 0 <= s < e <= len(transcript_text) and transcript_text[s:e] == raw_text:
            span = location.source_span
    if span is None:
        idx = transcript_text.find(raw_text)
        if idx >= 0:
            span = LocationSourceSpan(start=idx, end=idx + len(raw_text))

    return location.model_copy(update={"raw_text": raw_text, "source_span": span})


def _build_patient_location_from_llm(
    transcript_text: str,
    raw_text: str | None,
    components: dict | None,
    confidence: float,
    needs_confirmation: bool,
) -> PatientLocation | None:
    """Turn a strict LLM location payload into a canonical PatientLocation.

    The LLM returns loose fields (raw_text + city/district/street/landmark). We
    trust raw_text only when it appears in the transcript, and we compute
    source_span in code so the LLM doesn't have to emit correct offsets.
    """
    text = _normalize_string(raw_text)
    if not text:
        return None
    idx = transcript_text.find(text)
    span: LocationSourceSpan | None = None
    if idx >= 0:
        span = LocationSourceSpan(start=idx, end=idx + len(text))

    comps = components or {}
    return PatientLocation(
        raw_text=text,
        source_span=span,
        components=LocationComponents(
            street=_normalize_string(comps.get("street")),
            district=_normalize_string(comps.get("district")),
            city=_normalize_string(comps.get("city")),
            landmark=_normalize_string(comps.get("landmark")),
            governorate=_normalize_string(comps.get("governorate")),
        ),
        geocode=None,
        confidence=max(0.0, min(1.0, float(confidence or 0.0))),
        needs_confirmation=bool(needs_confirmation),
    )


class _ExtractionOnlyResult(BaseModel):
    highlights: list[HighlightItem]
    medical_entities: MedicalEntities


class _LocationOnlyResult(BaseModel):
    """Loose parser for the LLM's location payload.

    We intentionally accept a flat shape (raw_text + components as flat keys)
    so the LLM's job stays small. `_build_patient_location_from_llm` then
    converts this into a canonical `PatientLocation` and re-anchors offsets
    against the transcript.
    """

    raw_text: str | None = None
    city: str | None = None
    district: str | None = None
    street: str | None = None
    landmark: str | None = None
    governorate: str | None = None
    confidence: float = 0.0
    needs_confirmation: bool = True


class _RealtimeAnalysisPayload(BaseModel):
    """Loose LLM payload used by the realtime path.

    Matches the full shape the live prompt asks for but with a loose location
    sub-object. The canonical `PatientLocation` is constructed afterwards so
    offsets are computed from the transcript and mis-shaped fields don't trip
    validation.
    """

    highlights: list[HighlightItem] = Field(default_factory=list)
    medical_entities: MedicalEntities = Field(default_factory=MedicalEntities)
    triage: TriageAssessment = Field(default_factory=TriageAssessment)
    patient_location: _LocationOnlyResult = Field(default_factory=_LocationOnlyResult)
    meta: AnalysisMeta = Field(default_factory=AnalysisMeta)


class _TriageOnlyResult(BaseModel):
    triage: TriageAssessment


class _StrictHighlightItem(BaseModel):
    label: str = Field(description="Arabic or close-to-transcript label, not English unless the transcript uses English.")
    canonical_label: str = Field(description="Canonical medical label, preferably Arabic when possible.")
    span_text: str = Field(description="Exact substring copied from the transcript.")
    start: int | None = Field(default=None, description="Start character index if known, else null.")
    end: int | None = Field(default=None, description="End character index if known, else null.")
    severity: Literal["high", "medium", "low"]
    negated: bool
    uncertain: bool
    current: bool


class _StrictMedicalEntityItem(BaseModel):
    canonical_label: str = Field(description="Canonical medical label.")
    spoken_text: str = Field(description="Exact wording from the transcript.")
    severity: Literal["high", "medium", "low"]
    negated: bool
    uncertain: bool
    current: bool
    speaker: Literal["patient", "caller", "bystander", "unknown"] = Field(
        description="Who the entity refers to."
    )


class _StrictPatientState(BaseModel):
    consciousness: Literal["unknown", "alert", "reduced", "unresponsive"]
    breathing: Literal["unknown", "normal", "distressed", "absent"]
    bleeding: Literal["unknown", "none", "minor", "active", "severe"]


class _StrictMedicalEntities(BaseModel):
    symptoms: list[_StrictMedicalEntityItem]
    injuries: list[_StrictMedicalEntityItem]
    patient_state: _StrictPatientState
    risk_factors: list[str]
    mechanism_of_injury: list[str]
    resolved_clues: list[str]
    timeline_clues: list[str]


class _StrictExtractionOnlyResult(BaseModel):
    highlights: list[_StrictHighlightItem]
    medical_entities: _StrictMedicalEntities


class _StrictLocationAssessment(BaseModel):
    """Strict location shape we force the LLM to emit.

    `raw_text` is the exact current-incident location phrase from the
    transcript. Components are optional. Offsets are not required from the
    LLM — we compute them from `raw_text` against the transcript.
    """

    raw_text: str | None = Field(description="Exact current incident location phrase copied from the transcript, or null.")
    city: str | None
    district: str | None
    street: str | None
    landmark: str | None
    governorate: str | None = None
    confidence: float = Field(description="0 to 1 confidence score. Required even when uncertain.")
    needs_confirmation: bool


class _StrictLocationOnlyResult(BaseModel):
    location: _StrictLocationAssessment


class _StrictTriageAssessment(BaseModel):
    level: Literal["red", "yellow", "green"]
    confidence: float = Field(description="0 to 1 confidence score. Must not be omitted.")
    reasoning: list[str] = Field(description="At least one grounded reason.")
    needs_confirmation: bool


class _StrictTriageOnlyResult(BaseModel):
    triage: _StrictTriageAssessment


class AITriageAnalysisService:
    def __init__(
        self,
        ollama_base_url: str | None = None,
        ollama_model: str | None = None,
        preferred_models: Sequence[str] | None = None,
    ) -> None:
        settings = get_settings()
        self.ollama_base_url = ollama_base_url or settings.ollama_base_url
        self.ollama_model = ollama_model or settings.ollama_model
        self.preferred_models = list(preferred_models) if preferred_models else None

    def _candidate_base_urls(self) -> list[str]:
        candidates: list[str] = []
        parsed = urlparse(self.ollama_base_url)
        gateway_candidate: str | None = None
        if (
            _running_in_wsl()
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            gateway_ip = _wsl_windows_gateway_ip()
            if gateway_ip:
                gateway_candidate = urlunparse(parsed._replace(netloc=f"{gateway_ip}:{parsed.port or 11434}"))

        preferred = [_LAST_GOOD_OLLAMA_BASE_URL]
        if gateway_candidate:
            preferred.append(gateway_candidate)
        preferred.append(self.ollama_base_url)

        for candidate in preferred:
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _candidate_models(self) -> list[str]:
        preferred = self.preferred_models or [
            _LAST_GOOD_OLLAMA_MODEL,
            "qwen2.5:14b",
            "qwen2.5:7b",
            self.ollama_model,
        ]
        seen: set[str] = set()
        candidates: list[str] = []
        for model in preferred:
            if model and model not in seen:
                seen.add(model)
                candidates.append(model)
        return candidates

    def warm_realtime_models(self) -> None:
        global _LAST_GOOD_OLLAMA_BASE_URL, _LAST_GOOD_OLLAMA_MODEL
        preferred_models = ["qwen2.5:7b", "qwen2.5:14b", self.ollama_model]
        now = time.monotonic()

        for base_url in self._candidate_base_urls():
            for model_name in preferred_models:
                if not model_name:
                    continue
                cache_key = (base_url, model_name)
                last_warmed = _LAST_WARMED_AT.get(cache_key, 0.0)
                if now - last_warmed < _WARMUP_COOLDOWN_SECONDS:
                    continue

                payload = json.dumps(
                    {
                        "model": model_name,
                        "prompt": "جاهز",
                        "stream": False,
                        "keep_alive": "15m",
                        "options": {
                            "num_predict": 1,
                            "temperature": 0,
                        },
                    }
                ).encode("utf-8")

                req = request.Request(
                    f"{base_url.rstrip('/')}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                try:
                    with request.urlopen(req, timeout=20) as response:
                        response.read()
                    _LAST_WARMED_AT[cache_key] = time.monotonic()
                    _LAST_GOOD_OLLAMA_BASE_URL = base_url
                    _LAST_GOOD_OLLAMA_MODEL = model_name
                    logger.info("Warmed realtime Ollama model=%s via %s", model_name, base_url)
                except Exception as exc:
                    logger.warning("Warmup failed for model=%s via %s: %s", model_name, base_url, exc)

    def _call_structured_llm(
        self,
        *,
        result_model: type[BaseModel],
        system_prompt: str,
        user_prompt: str,
        require_non_empty: bool = False,
        preferred_models: Sequence[str] | None = None,
        timeout_seconds: int = 90,
        num_predict: int | None = None,
        num_ctx: int = 4096,
        keep_alive: str = "30m",
    ) -> BaseModel:
        """Call Ollama for a structured (JSON-schema) response.

        Latency knobs (all optional, safe defaults):

        * ``num_predict`` caps generated tokens. Set a tight value
          (e.g. 256) for small outputs like the triage-only call so a
          chatty model can't spend seconds over-generating. Leave
          ``None`` (uncapped) for larger payloads (full extraction)
          to avoid truncating the JSON.
        * ``num_ctx`` bounds the context window. Live transcripts are
          short, so 4096 comfortably fits prompt + transcript while
          being cheaper than the model's 32k default.
        * ``keep_alive`` keeps the model resident in VRAM between
          calls so we don't pay a cold reload on the next bubble.
        """
        global _LAST_GOOD_OLLAMA_BASE_URL, _LAST_GOOD_OLLAMA_MODEL
        schema = result_model.model_json_schema()
        last_error: Exception | None = None

        options: dict[str, Any] = {
            "temperature": 0.1,
            "num_ctx": num_ctx,
        }
        if num_predict is not None:
            options["num_predict"] = num_predict

        for base_url in self._candidate_base_urls():
            model_candidates = (
                list(preferred_models)
                if preferred_models
                else self._candidate_models()
            )
            for model_name in model_candidates:
                payload = json.dumps(
                    {
                        "model": model_name,
                        "system": system_prompt,
                        "prompt": user_prompt,
                        "stream": False,
                        "format": schema,
                        "keep_alive": keep_alive,
                        "options": options,
                    }
                ).encode("utf-8")

                req = request.Request(
                    f"{base_url.rstrip('/')}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                try:
                    with request.urlopen(req, timeout=timeout_seconds) as response:
                        body = response.read().decode("utf-8")
                    outer = json.loads(body)
                    raw_response = outer.get("response", "{}")
                    parsed = (
                        result_model.model_validate_json(raw_response)
                        if isinstance(raw_response, str)
                        else result_model.model_validate(raw_response)
                    )
                    if require_non_empty and self._looks_empty(parsed):
                        logger.info(
                            "Structured LLM response from %s via %s was empty, trying next model",
                            model_name,
                            base_url,
                        )
                        continue
                    _LAST_GOOD_OLLAMA_BASE_URL = base_url
                    _LAST_GOOD_OLLAMA_MODEL = model_name
                    logger.info("Structured LLM response succeeded with model=%s via %s", model_name, base_url)
                    return parsed
                except Exception as exc:
                    last_error = exc
                    logger.warning("Structured LLM call failed for model=%s via %s: %s", model_name, base_url, exc)
                    continue

        if last_error:
            logger.warning("All structured LLM attempts failed; returning empty model: %s", last_error)
        return self._empty_result_for(result_model)

    @staticmethod
    def _looks_empty(result: BaseModel) -> bool:
        if isinstance(result, (_ExtractionOnlyResult, _StrictExtractionOnlyResult)):
            entities = result.medical_entities
            return (
                len(result.highlights) == 0
                and len(entities.symptoms) == 0
                and len(entities.injuries) == 0
                and len(entities.risk_factors) == 0
                and len(entities.mechanism_of_injury) == 0
                and len(entities.resolved_clues) == 0
                and len(entities.timeline_clues) == 0
                and entities.patient_state.consciousness == "unknown"
                and entities.patient_state.breathing == "unknown"
                and entities.patient_state.bleeding == "unknown"
            )
        if isinstance(result, (_TriageOnlyResult, _StrictTriageOnlyResult)):
            return (
                result.triage.level == "green"
                and result.triage.confidence == 0.0
                and len(result.triage.reasoning) == 0
            )
        return False

    @staticmethod
    def _empty_result_for(result_model: type[BaseModel]) -> BaseModel:
        if result_model is _StrictExtractionOnlyResult:
            return _StrictExtractionOnlyResult(
                highlights=[],
                medical_entities=_StrictMedicalEntities(
                    symptoms=[],
                    injuries=[],
                    patient_state=_StrictPatientState(
                        consciousness="unknown",
                        breathing="unknown",
                        bleeding="unknown",
                    ),
                    risk_factors=[],
                    mechanism_of_injury=[],
                    resolved_clues=[],
                    timeline_clues=[],
                ),
            )
        if result_model is _StrictLocationOnlyResult:
            return _StrictLocationOnlyResult(
                location=_StrictLocationAssessment(
                    raw_text=None,
                    city=None,
                    district=None,
                    street=None,
                    landmark=None,
                    governorate=None,
                    confidence=0.0,
                    needs_confirmation=True,
                )
            )
        if result_model is _StrictTriageOnlyResult:
            return _StrictTriageOnlyResult(
                triage=_StrictTriageAssessment(
                    level="green",
                    confidence=0.0,
                    reasoning=[],
                    needs_confirmation=True,
                )
            )
        return result_model()

    @staticmethod
    def _to_extraction_result(strict_result: _StrictExtractionOnlyResult) -> _ExtractionOnlyResult:
        return _ExtractionOnlyResult.model_validate(strict_result.model_dump(mode="json"))

    @staticmethod
    def _to_location_result(strict_result: _StrictLocationOnlyResult) -> _LocationOnlyResult:
        loc = strict_result.location
        return _LocationOnlyResult(
            raw_text=loc.raw_text,
            city=loc.city,
            district=loc.district,
            street=loc.street,
            landmark=loc.landmark,
            governorate=loc.governorate,
            confidence=loc.confidence,
            needs_confirmation=loc.needs_confirmation,
        )

    @staticmethod
    def _patient_location_from_loose(
        transcript_text: str, loose: _LocationOnlyResult
    ) -> PatientLocation | None:
        return _build_patient_location_from_llm(
            transcript_text,
            raw_text=loose.raw_text,
            components={
                "street": loose.street,
                "district": loose.district,
                "city": loose.city,
                "landmark": loose.landmark,
                "governorate": loose.governorate,
            },
            confidence=loose.confidence,
            needs_confirmation=loose.needs_confirmation,
        )

    @staticmethod
    def _to_triage_result(strict_result: _StrictTriageOnlyResult) -> _TriageOnlyResult:
        return _TriageOnlyResult.model_validate(strict_result.model_dump(mode="json"))

    def _extract_medical_entities(self, transcript_text: str) -> _ExtractionOnlyResult:
        system_prompt = (
            "You are an Arabic EMS medical extraction engine.\n"
            "Read the transcript and extract only grounded medical entities and exact spoken evidence.\n"
            "Support Saudi dialects (Najdi, Hijazi, Khaleeji) and MSA.\n"
            "Return JSON only.\n"
            "Do not classify overall triage here.\n"
            "Do not guess missing symptoms.\n"
            "Do NOT infer mechanism_of_injury from the type of injury. mechanism_of_injury is allowed only when the caller actually SAYS an accident/mechanism word (collision, crash, fall, stab, gunshot, fire, rollover, struck).\n"
            "If something is negated or resolved, mark it correctly.\n"
            "All label, canonical_label, and evidence strings MUST be in Arabic characters only."
        )
        user_prompt = (
            "Return JSON with only these keys: highlights and medical_entities.\n"
            "Each highlight span_text must be an exact substring of the transcript.\n"
            "canonical_label and label must be Arabic; never use English medical terms (no amputation, no severe trauma, no uncontrolled bleeding).\n"
            "Every entity field must be filled; use empty lists instead of omitting keys.\n"
            "Use only allowed speaker values: patient, caller, bystander, unknown.\n"
            "Use only allowed patient_state values:\n"
            "- consciousness: unknown | alert | reduced | unresponsive\n"
            "- breathing: unknown | normal | distressed | absent\n"
            "- bleeding: unknown | none | minor | active | severe\n\n"
            "POSITIVE mechanism example (word for accident IS spoken):\n"
            "Transcript: صار حادث سيارة قوي والسيارة انقلبت وفيه نزيف من الرجل\n"
            "mechanism_of_injury includes \"حادث\" and \"انقلاب\" (both grounded in spoken words).\n\n"
            "NEGATIVE mechanism example (no accident word spoken):\n"
            "Transcript: رجلي انقطعت فيني نزيف حاد\n"
            "injuries include بتر رجل and نزيف حاد but mechanism_of_injury MUST stay [] because no accident word was said.\n\n"
            "Resolved example:\n"
            "Transcript: كان يختنق قبل شوي لكن الحين يتنفس\n"
            "The choking entry must have current=false.\n\n"
            f"Transcript:\n{transcript_text}"
        )
        strict_result = self._call_structured_llm(
            result_model=_StrictExtractionOnlyResult,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            require_non_empty=True,
        )
        return self._to_extraction_result(strict_result)

    def _extract_location(self, transcript_text: str) -> _LocationOnlyResult:
        system_prompt = (
            "You are an Arabic EMS location extraction engine.\n"
            "Extract only the current incident location.\n"
            "Ignore historical places, destinations, and hospitals unless they are the current emergency scene.\n"
            "Return JSON only."
        )
        user_prompt = (
            "Return JSON with one key: location.\n"
            "The location object has these fields: raw_text, city, district, street, landmark, governorate, confidence, needs_confirmation.\n"
            "If there is no grounded current location, set raw_text and every component to null.\n"
            "Always provide confidence and needs_confirmation.\n"
            "If street, district, city, or landmark is extracted, raw_text MUST be an exact substring of the transcript covering the location phrase.\n"
            "Example: احنا عند طريق الملك فهد قريب من النخيل مول بالرياض should extract the road, landmark, and city, with raw_text='طريق الملك فهد قريب من النخيل مول بالرياض'.\n\n"
            f"Transcript:\n{transcript_text}"
        )
        strict_result = self._call_structured_llm(
            result_model=_StrictLocationOnlyResult,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            require_non_empty=False,
        )
        return self._to_location_result(strict_result)

    def _recommend_triage(
        self,
        transcript_text: str,
        extraction: _ExtractionOnlyResult,
        location: _LocationOnlyResult,
    ) -> _TriageOnlyResult:
        system_prompt = (
            "You are an Arabic EMS triage recommendation engine.\n"
            "Given the transcript and extracted medical evidence, recommend red, yellow, or green.\n"
            "This is a dispatcher decision-support recommendation, not a chatbot answer.\n"
            "Return JSON only.\n"
            "Do not ignore clearly dangerous symptoms like inability to breathe, major bleeding, unresponsiveness, or severe chest pain.\n"
            "All reasoning strings MUST be written in Arabic characters only. Never output English."
        )
        user_prompt = (
            "Return JSON with only one key: triage.\n"
            "Base the decision strictly on the transcript and extracted evidence.\n"
            "Always provide a numeric confidence between 0 and 1.\n"
            "Always provide at least one grounded reasoning string written in Arabic only.\n"
            "Never invent incidents that were not spoken. Do not claim a traffic accident unless it is in the transcript.\n"
            "Example: المريض ما يقدر يتنفس وعنده ألم شديد في صدره -> likely red; reasoning e.g. ['عجز عن التنفس', 'ألم صدر شديد'].\n"
            "Example: كان يختنق قبل شوي لكن الحين يتنفس -> not active red.\n\n"
            f"Transcript:\n{transcript_text}\n\n"
            f"Extracted medical evidence:\n{json.dumps(extraction.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            f"Extracted location:\n{json.dumps(location.model_dump(mode='json'), ensure_ascii=False)}"
        )
        strict_result = self._call_structured_llm(
            result_model=_StrictTriageOnlyResult,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            require_non_empty=True,
        )
        return self._to_triage_result(strict_result)

    def analyze_transcript(self, transcript_text: str) -> AITriageAnalysis:
        transcript_text = transcript_text.strip()
        logger.info("AI triage analysis requested (text_length=%d)", len(transcript_text))
        if len(transcript_text) < 8:
            return _empty_analysis()

        with ThreadPoolExecutor(max_workers=2) as executor:
            extraction_future = executor.submit(self._extract_medical_entities, transcript_text)
            location_future = executor.submit(self._extract_location, transcript_text)
            extraction = extraction_future.result()
            location = location_future.result()
        triage = self._recommend_triage(transcript_text, extraction, location)

        aligned_highlights = _align_highlights(transcript_text, extraction.highlights)
        grounded_highlights = _sanitize_highlight_items(transcript_text, aligned_highlights)
        grounded_entities = _sanitize_medical_entities(
            extraction.medical_entities, transcript_text
        )
        grounded_triage = _sanitize_triage_assessment(triage.triage)

        return AITriageAnalysis(
            highlights=grounded_highlights,
            medical_entities=grounded_entities,
            triage=grounded_triage,
            patient_location=self._patient_location_from_loose(transcript_text, location),
            meta=AnalysisMeta(),
        )

    def analyze_transcript_realtime(self, transcript_text: str) -> AITriageAnalysis:
        transcript_text = transcript_text.strip()
        logger.info("Realtime AI triage analysis requested (text_length=%d)", len(transcript_text))
        if len(transcript_text) < 8:
            return _empty_analysis()

        # The realtime LLM does a single job: triage (level + confidence
        # + short Arabic reasoning). Highlighting is owned by the fast
        # deterministic layer and location by the fast regex extractor,
        # so this path deliberately skips the heavier full-extraction
        # call. Keeping the realtime call small minimises the latency
        # before the triage badge appears.
        triage_system_prompt = (
            "You are a realtime Arabic EMS triage engine.\n"
            "Return JSON only with a triage object.\n"
            "Base the decision strictly on the transcript.\n"
            "Severe trauma, amputation, uncontrolled bleeding, major collisions, rollover, pedestrian struck, falls from height, inability to breathe, unresponsiveness, and severe chest pain are red.\n"
            "Do not downgrade obviously critical trauma.\n"
            "All reasoning strings MUST be written in Arabic only. Do not use English."
        )
        triage_user_prompt = (
            "Return JSON with only one key: triage.\n"
            "Always provide level, confidence, reasoning (Arabic only), and needs_confirmation.\n"
            "Each reasoning string must be a short Arabic sentence grounded in the transcript.\n"
            "Never emit reasoning in English. Never invent incidents that were not spoken.\n\n"
            "Examples:\n"
            "- رجلي انقطعت وعندي نزيف -> red; reasoning e.g. ['بتر في الطرف', 'نزيف نشط']\n"
            "- فيه نزيف شديد من رجله -> red; reasoning e.g. ['نزيف شديد غير متحكم به']\n"
            "- ما يقدر يتنفس -> red; reasoning e.g. ['عجز عن التنفس']\n"
            "- كان يختنق قبل شوي لكن الحين يتنفس -> yellow or green depending on active danger, not red if resolved.\n\n"
            f"Transcript:\n{transcript_text}"
        )

        realtime_triage = self._to_triage_result(
            self._call_structured_llm(
                result_model=_StrictTriageOnlyResult,
                system_prompt=triage_system_prompt,
                user_prompt=triage_user_prompt,
                require_non_empty=True,
                preferred_models=["qwen2.5:7b", "qwen2.5:14b", self.ollama_model],
                timeout_seconds=12,
                # Small output (just the triage object + short Arabic
                # reasoning): a tight cap stops the model from
                # over-generating and shaves latency off the badge.
                num_predict=256,
            )
        )

        # Highlights + location are owned by the fast layer; the LLM
        # contributes neither here. ``case_state_service`` keeps the
        # fast-grounded highlights and the fast regex location. We let
        # ``highlights`` / ``medical_entities`` / ``patient_location``
        # take their model defaults (empty list / empty MedicalEntities
        # / None) — passing a bare list for medical_entities fails
        # validation since the field is a MedicalEntities model.
        return AITriageAnalysis(
            triage=_sanitize_triage_assessment(realtime_triage.triage),
            meta=AnalysisMeta(),
        )
