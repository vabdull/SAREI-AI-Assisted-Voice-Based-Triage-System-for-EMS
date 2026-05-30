"""
fast_decision_service — deterministic, LLM-free fast path.

Scope
-----
Given a chunk of transcript text (the freshly-transcribed ASR bubble)
and the current merged transcript, produce everything the dispatcher
UI must see within ~20ms of speech:

* the fast triage result (red/yellow/green + ESI),
* the matched keyword set with character-level spans,
* a revision-grounded patient-location candidate,
* the patient-count hint.

Everything in here is deterministic and rule-based. The LLM lives in
``enrichment_service``. This module MUST NOT import Ollama, urllib, or
``AITriageAnalysisService``.

Why this exists
---------------
Before the refactor the fast decisions were scattered across the
frontend (``buildFastPathHighlights``, ``extractPreviewLocation``,
``inferPatientsFast``) and the route handler (``_extract_grounded_
location_from_transcript``, inline regex). That meant:

* two different implementations could disagree;
* the UI owned business logic that belongs on the server;
* the same work ran on every poll + every WS broadcast.

Here we centralise. The route handler calls
``fast_decision_service.process`` once per chunk; everything else is
derived.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from backend.app.schemas.live_state import HighlightPayload
from backend.app.schemas.location import LocationComponents, PatientLocation
from backend.app.services.observability import log_stage, stage_timer
from backend.app.triage_engine import get_triage_engine
from backend.app.triage_engine.models import TriageFastResult, TriageMatch
from backend.app.triage_engine.normalization import (
    find_phrase_token_aware,
)

logger = logging.getLogger(__name__)


# ── Location extraction ────────────────────────────────────────────────
#
# The keyword bank / pipeline config is the source of truth for which
# Arabic phrases trigger a location capture. The patterns below are
# COMPILED LAZILY from `configs/triage/pipeline.yaml >
# location_extraction`. Pre-fix this module hardcoded a single set of
# Najdi/Hijazi phrases; now dispatch can extend coverage by editing
# YAML and the engine will pick it up on next call.

_FALLBACK_LOCATION_PREFIXES = (
    "عند", "جنب", "بجنب", "جمب", "قدام", "مقابل", "ورا", "وراء",
    "قريب من", "بالقرب من", "هنا", "هناك", "داخل", "برا",
)
_FALLBACK_LOCATION_POI = (
    "شارع", "طريق", "حي", "مخرج", "جسر", "كبري", "دوار",
    "اشاره", "اشارة", "محطه", "محطة", "مسجد", "مدرسه", "مدرسة",
    "مستشفى", "ميدان", "ساحة", "مجمع", "بناية", "عمارة", "فيلا",
    "مول", "سوبر", "بقالة", "تقاطع", "نهاية", "بداية",
)
_FALLBACK_LOCATION_STOP_WORDS = frozenset({
    "صار", "صاير", "صاروا",
    "عليه", "عليها", "عليهم",
    "حادث", "الحادث",
    "نزيف", "ينزف",
    "يتنفس", "يتكلم",
    "وفيه", "فيه", "وفي",
    "هو", "هي",
    "واحد", "وحده",
    "شخص", "رجال", "حرمه", "مرة",
})


def _location_config() -> dict:
    """Read location_extraction config from pipeline.yaml.

    Falls back to the in-code constants if the engine is unavailable
    (defensive: never crash the live path on a config read).
    """
    try:
        from backend.app.triage_engine import get_triage_engine

        return get_triage_engine().bank.pipeline.get("location_extraction", {}) or {}
    except Exception:  # pragma: no cover - bootstrap-only path
        return {}


def _build_location_patterns() -> tuple[tuple[re.Pattern[str], ...], frozenset[str]]:
    """Compile the location regexes from YAML.

    Recomputed once per process via the cache below; tests reset the
    cache by setting ``_LOCATION_CACHE = None``.
    """
    cfg = _location_config()
    prefixes = tuple(cfg.get("prefix_words") or _FALLBACK_LOCATION_PREFIXES)
    pois = tuple(cfg.get("poi_words") or _FALLBACK_LOCATION_POI)
    stop_words = frozenset(
        cfg.get("trailing_stop_words") or _FALLBACK_LOCATION_STOP_WORDS
    )

    # Sort longer phrases first so multi-word prefixes ("قريب من") match
    # before their single-word components.
    prefix_re = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    poi_re = "|".join(re.escape(p) for p in sorted(pois, key=len, reverse=True))

    patterns = (
        re.compile(
            rf"(?:{prefix_re})\s+[^\s،,.]+(?:\s+[^\s،,.]+){{0,5}}"
        ),
        re.compile(
            r"(?:في|بحي)\s+(?:حي\s+)?[^\s،,.]+(?:\s+[^\s،,.]+){0,4}"
        ),
        re.compile(
            rf"(?:{poi_re})\s+[^\s،,.]+(?:\s+[^\s،,.]+){{0,4}}"
        ),
    )
    return patterns, stop_words


_LOCATION_CACHE: tuple[tuple[re.Pattern[str], ...], frozenset[str]] | None = None


def _get_location_patterns() -> tuple[tuple[re.Pattern[str], ...], frozenset[str]]:
    global _LOCATION_CACHE
    if _LOCATION_CACHE is None:
        _LOCATION_CACHE = _build_location_patterns()
    return _LOCATION_CACHE


def _trim_location_candidate(raw_text: str) -> str:
    _, stop_words = _get_location_patterns()
    tokens = raw_text.strip().split()
    while tokens and tokens[-1] in stop_words:
        tokens.pop()
    return " ".join(tokens).strip()


def extract_grounded_location(transcript_text: str) -> PatientLocation | None:
    """Regex-only location extraction.

    Never calls an LLM. Never blocks. Returns ``None`` when the
    transcript doesn't contain a recognisable address phrase; the
    enrichment path may still produce a richer location later.
    """
    transcript_text = (transcript_text or "").strip()
    if len(transcript_text) < 8:
        return None
    patterns, _stop_words = _get_location_patterns()
    for pattern in patterns:
        match = pattern.search(transcript_text)
        if not match:
            continue
        location_text = _trim_location_candidate(match.group(0))
        if len(location_text) < 4:
            continue
        return PatientLocation(
            raw_text=location_text,
            source_span={
                "start": match.start(),
                "end": match.start() + len(location_text),
            },
            components=LocationComponents(),
            geocode=None,
            confidence=0.72,
            needs_confirmation=True,
        )
    return None


# ── Patient-count extraction ───────────────────────────────────────────

_PATIENT_COUNT_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\b(?:مصابين|مصابون|جرحى)\s+(\d{1,3})\b"), 0),
    (re.compile(r"\b(\d{1,3})\s+(?:مصابين|مصابون|جرحى|اشخاص|اشخاس|افراد)\b"), 0),
    (re.compile(r"\b(?:في|فيه|يوجد|عندي|معي)\s+(\d{1,3})\s+(?:مصاب|جريح|شخص)"), 0),
)
_SPELLED_ARABIC_COUNTS: dict[str, int] = {
    # 1
    "واحد": 1, "واحدة": 1, "وحده": 1, "وحدة": 1,
    # 2
    "اثنين": 2, "اثنان": 2, "ثنين": 2, "اتنين": 2, "اثنتين": 2,
    # 3 — covers both the formal feminine (ثلاثة) used with masculine
    # plurals in MSA AND the bare cardinal (ثلاث) heard in Najdi /
    # Hijazi speech ("ثلاث مصابين"). The bare form was missing and
    # caused every Najdi 3-victim call to silently report
    # ``patient_count=None`` — i.e. dispatch under-resourced.
    "ثلاث": 3, "ثلاثه": 3, "ثلاثة": 3, "تلاته": 3, "تلاتة": 3,
    # 4
    "اربع": 4, "اربعه": 4, "اربعة": 4, "أربع": 4, "أربعة": 4,
    # 5
    "خمس": 5, "خمسه": 5, "خمسة": 5,
    # 6
    "ست": 6, "ستة": 6, "سته": 6,
    # 7
    "سبع": 7, "سبعة": 7, "سبعه": 7,
    # 8
    "ثمان": 8, "ثماني": 8, "ثمانية": 8, "ثمانيه": 8, "تماني": 8, "تمانية": 8,
    # 9
    "تسع": 9, "تسعة": 9, "تسعه": 9,
    # 10
    "عشر": 10, "عشرة": 10, "عشره": 10,
}

# Sorted longest-first so the alternation prefers more-specific forms
# (e.g. matches "ثلاثة" before falling back to "ثلاث"). Without this,
# Python's regex picks the leftmost-listed alternative which can be
# the shorter one, leading to misreads on substrings.
_SPELLED_COUNT_RE = re.compile(
    r"\b("
    + "|".join(sorted(_SPELLED_ARABIC_COUNTS, key=len, reverse=True))
    + r")\s+(?:مصاب|جريح|شخص|اشخاص|اشخاس|مصابين|جرحى|افراد)"
)


# Age expressions whose number words must NEVER be read as a patient
# count. e.g. "عمري ثلاثة وعشرين" (I'm 23) — the "ثلاثة" is part of an
# age, not "3 injured". We blank out these spans before counting so a
# stray count noun later in the sentence can't latch onto the age digit
# / word. Covers: an age trigger followed by number words, and a
# spelled tens compound ("... وعشرين / وثلاثين ...") which only appears
# in ages within EMS speech (counts above ~20 are said with digits).
# Number words that can appear inside a spoken age, kept tight so the
# strip never swallows a following count phrase ("... في اربع مصابين").
_AGE_NUMBER_WORD = (
    r"(?:\d{1,3}|واحد|واحده|اثنين|اثنان|اتنين|ثلاثه|ثلاثة|ثلاث|اربعه|اربعة|اربع|"
    r"خمسه|خمسة|خمس|سته|ستة|ست|سبعه|سبعة|سبع|ثمانيه|ثمانية|ثمان|تسعه|تسعة|تسع|"
    r"عشره|عشرة|عشر|عشرين|ثلاثين|اربعين|خمسين|ستين|سبعين|ثمانين|تسعين|مئه|مئة|"
    r"و)"
)
_AGE_TRIGGER_WORD = r"(?:عمره|عمرها|عمري|العمر|عمر|بعمر|عنده|عندها|يبلغ|تبلغ)"

_AGE_SPAN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Age trigger + one or more number words, optionally ending in a
    # year unit. Only number words (not arbitrary text) are consumed, so
    # a later "اربع مصابين" is preserved for the count pass.
    re.compile(
        _AGE_TRIGGER_WORD
        + r"(?:\s+" + _AGE_NUMBER_WORD + r"){1,4}"
        + r"(?:\s+(?:سنه|سنوات|سنة|عام|عاما))?"
    ),
    # Spelled tens compound on its own (21–99) — only ever an age here.
    re.compile(
        r"\b(?:" + _AGE_NUMBER_WORD + r")\s+و(?:عشرين|ثلاثين|اربعين|خمسين|ستين|سبعين|ثمانين|تسعين)\b"
    ),
)


def _strip_age_spans(text: str) -> str:
    """Blank out age expressions so they can't be miscounted as patients."""
    for pattern in _AGE_SPAN_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def infer_patient_count(transcript_text: str) -> int | None:
    """Deterministic patient-count inference from the transcript.

    Grounded only on the transcript — no LLM. Returns ``None`` when
    the transcript doesn't explicitly mention a count.
    """
    text = (transcript_text or "").strip()
    if not text:
        return None
    # Remove age expressions first: "عمري ثلاثة وعشرين سنة" must not be
    # read as "3 injured" by the spelled-count matcher below.
    text = _strip_age_spans(text)
    for pattern, group_idx in _PATIENT_COUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                value = int(match.group(group_idx + 1 if group_idx == 0 else group_idx))
            except (IndexError, ValueError):
                continue
            if 1 <= value <= 99:
                return value
    spelled = _SPELLED_COUNT_RE.search(text)
    if spelled:
        return _SPELLED_ARABIC_COUNTS.get(spelled.group(1))
    return None


# ── Patient demographics extraction (name / age / gender) ──────────────
#
# Deterministic, LLM-free. The ASR transcript usually spells numbers as
# Arabic words ("خمسة وأربعين سنة") rather than digits, so age parsing
# supports both digits and spelled Arabic numerals. Everything here is
# best-effort: when nothing matches we return ``None`` and the dispatcher
# can still type it in via the Edit Call Info form.

_AR_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0640]")


def _normalize_ar(text: str) -> str:
    """Light Arabic normalization for robust keyword matching."""
    text = _AR_DIACRITICS.sub("", text or "")
    text = (
        text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
        .replace("ة", "ه")
    )
    return text


_AR_UNITS: dict[str, int] = {
    "صفر": 0, "واحد": 1, "واحده": 1, "اثنين": 2, "اثنان": 2, "اتنين": 2,
    "ثلاثه": 3, "ثلاث": 3, "تلاته": 3, "اربعه": 4, "اربع": 4, "خمسه": 5,
    "خمس": 5, "سته": 6, "سبعه": 7, "سبع": 7, "ثمانيه": 8, "ثمان": 8,
    "تمانيه": 8, "تسعه": 9, "تسع": 9,
}
_AR_TEENS: dict[str, int] = {
    "عشره": 10, "احدعشر": 11, "احد عشر": 11, "اثناعشر": 12, "اثنا عشر": 12,
    "ثلاثطعش": 13, "ثلاثه عشر": 13, "اربعطعش": 14, "اربعه عشر": 14,
    "خمسطعش": 15, "خمسه عشر": 15, "ستطعش": 16, "سته عشر": 16,
    "سبعطعش": 17, "سبعه عشر": 17, "ثمنطعش": 18, "ثمانيه عشر": 18,
    "تسعطعش": 19, "تسعه عشر": 19,
}
_AR_TENS: dict[str, int] = {
    "عشرين": 20, "ثلاثين": 30, "اربعين": 40, "خمسين": 50, "ستين": 60,
    "سبعين": 70, "ثمانين": 80, "تسعين": 90, "مايه": 100, "مئه": 100,
}


def _spelled_age_to_int(phrase: str) -> int | None:
    """Parse a normalized Arabic number phrase (0–120) into an int.

    Handles: standalone units/teens/tens, and ``<unit> و <ten>``
    compounds such as "خمسه واربعين" (45).
    """
    phrase = phrase.strip()
    if not phrase:
        return None
    if phrase in _AR_TEENS:
        return _AR_TEENS[phrase]
    if phrase in _AR_TENS:
        return _AR_TENS[phrase]
    if phrase in _AR_UNITS:
        return _AR_UNITS[phrase]
    # Compound: unit + (و) + ten, e.g. "خمسه و اربعين" / "خمسه واربعين".
    tokens = phrase.replace("و", " و ").split()
    unit_val: int | None = None
    ten_val: int | None = None
    for tok in tokens:
        if tok in _AR_UNITS and unit_val is None:
            unit_val = _AR_UNITS[tok]
        elif tok in _AR_TENS and ten_val is None:
            ten_val = _AR_TENS[tok]
    if ten_val is not None:
        return ten_val + (unit_val or 0)
    return None


_AGE_TRIGGER = r"(?:عمره|عمرها|عمري|العمر|عمر|بعمر|عنده|عندها|يبلغ|تبلغ)"
# Digit age: "عمره 45" or "45 سنة".
_AGE_DIGIT_RE = re.compile(
    rf"(?:{_AGE_TRIGGER}\s+)?(\d{{1,3}})\s*(?:سنه|سنوات|عام|عاما)?"
)
_AGE_DIGIT_TRIGGER_RE = re.compile(rf"{_AGE_TRIGGER}\s+(\d{{1,3}})")
# Spelled age: capture the Arabic words between an age trigger / before
# the unit word "سنة".
_AGE_SPELLED_BEFORE_UNIT_RE = re.compile(
    r"([\u0600-\u06FF\s]{2,40}?)\s+(?:سنه|سنوات|عام|عاما)"
)
_AGE_SPELLED_AFTER_TRIGGER_RE = re.compile(
    rf"{_AGE_TRIGGER}\s+([\u0600-\u06FF\s]{{2,40}})"
)


def _extract_age(raw: str, norm: str) -> int | None:
    # 1) Digit forms (most reliable).
    m = _AGE_DIGIT_TRIGGER_RE.search(norm)
    if not m:
        m = re.search(r"(\d{1,3})\s*(?:سنه|سنوات|عام|عاما)", norm)
    if m:
        try:
            val = int(m.group(1))
            if 0 < val <= 120:
                return val
        except ValueError:
            pass
    # 2) Spelled forms before the unit word "سنة".
    m = _AGE_SPELLED_BEFORE_UNIT_RE.search(norm)
    if m:
        val = _spelled_age_to_int(m.group(1).strip())
        if val is not None and 0 < val <= 120:
            return val
    # 3) Spelled forms right after an age trigger.
    m = _AGE_SPELLED_AFTER_TRIGGER_RE.search(norm)
    if m:
        val = _spelled_age_to_int(m.group(1).strip())
        if val is not None and 0 < val <= 120:
            return val
    return None


_GENDER_MALE = frozenset({
    "رجل", "راجل", "ذكر", "ولد", "شاب", "صبي", "رجال", "غلام",
})
_GENDER_FEMALE = frozenset({
    "امراه", "مراه", "انثي", "بنت", "فتاه", "سيده", "حرمه", "شابه", "طفله",
})


def _extract_gender(norm: str) -> str | None:
    """Return 'ذكر' / 'أنثى' based on the earliest gender cue."""
    tokens = norm.split()
    for tok in tokens:
        if tok in _GENDER_MALE:
            return "ذكر"
        if tok in _GENDER_FEMALE:
            return "أنثى"
    return None


_NAME_TRIGGER_RE = re.compile(
    r"(?:اسمه|اسمها|اسمي|الاسم|يسمي|تسمي|نسميه|اسم المريض|اسم المصاب)\s+"
    r"([\u0600-\u06FF]+(?:\s+[\u0600-\u06FF]+)?)"
)
# Words that are never a name even if they follow a name trigger.
_NAME_STOP_WORDS = frozenset({
    "هو", "هي", "في", "مو", "مش", "ما", "لا", "كان", "صار", "عمره",
    "عمرها", "سنه", "ذكر", "انثي", "رجل", "امراه",
})


def _extract_name(raw: str, norm: str) -> str | None:
    m = _NAME_TRIGGER_RE.search(raw)
    if not m:
        return None
    candidate = m.group(1).strip()
    parts: list[str] = []
    for tok in candidate.split():
        stem = _normalize_ar(tok)
        # Strip a leading "و" conjunction ("وهو", "وعمره") so we can
        # detect a clause break — but keep real names that simply start
        # with waw (e.g. "وليد") because their stem isn't a stop word.
        if stem.startswith("و") and len(stem) > 1:
            stem = stem[1:]
        if stem in _NAME_STOP_WORDS:
            break
        parts.append(tok)
        if len(parts) == 2:  # first + family name is enough
            break
    if not parts:
        return None
    return " ".join(parts)


@dataclass(frozen=True)
class PatientDemographics:
    name: str | None = None
    age: int | None = None
    gender: str | None = None

    def is_empty(self) -> bool:
        return self.name is None and self.age is None and self.gender is None


def extract_patient_demographics(transcript_text: str) -> PatientDemographics:
    """Best-effort deterministic name/age/gender extraction (Arabic)."""
    raw = (transcript_text or "").strip()
    if not raw:
        return PatientDemographics()
    norm = _normalize_ar(raw)
    return PatientDemographics(
        name=_extract_name(raw, norm),
        age=_extract_age(raw, norm),
        gender=_extract_gender(norm),
    )


# ── Highlight grounding ────────────────────────────────────────────────

def ground_highlights_from_matches(
    transcript_text: str,
    matches: list[TriageMatch],
) -> list[HighlightPayload]:
    """Turn fast-path matches into highlight payloads grounded in the
    current transcript using **token-aware** boundary checks.

    Each match can carry multiple evidence spans in normalized form.
    For every span we call :func:`find_phrase_token_aware` which:

    * tokenizes the raw transcript on Arabic word boundaries,
    * requires the bank phrase to align with whole transcript tokens
      (single token or a consecutive run for multi-token phrases),
    * tolerates a single conservative clitic prefix on the leading
      transcript token (و/ف/ب/ك/ل/ال) so ``والصداع`` still matches
      a ``صداع`` phrase, but only when the resulting stem is long
      enough to be meaningful (≥ 4 chars).

    Because the live transcript is append-only, when a symptom is
    repeated we want the FRESHLY-SPOKEN occurrence to highlight (so
    the dispatcher sees what they just heard), not the original one.
    We therefore ask the finder for the LAST token-aligned occurrence.

    A match is dropped silently if none of its spans token-align with
    the current transcript — that protects against painting text the
    dispatcher already corrected and against pre-fix false positives
    that lingered as cached match objects.
    """
    if not transcript_text or not matches:
        return []
    payloads: list[HighlightPayload] = []
    for m in matches:
        if m.negated:
            continue
        anchored = False
        for span in m.spans:
            span_text = span.text
            if not span_text:
                continue
            anchor = find_phrase_token_aware(
                transcript_text,
                span_text,
                occurrence="last",
                allow_clitic_prefix=True,
            )
            if anchor is None:
                log_stage(
                    stage="grounding_miss",
                    latency_ms=0.0,
                    case_id=0,
                    result_kind=m.canonical_label_ar,
                    needle=span_text,
                )
                continue
            raw_start, raw_end = anchor
            # ``raw_end`` is exclusive — slice gives the verbatim
            # substring the UI will paint, which may differ from the
            # normalized ``span_text`` (e.g. أ vs ا, or a leading
            # clitic such as "وال").
            raw_span_text = transcript_text[raw_start:raw_end]
            payloads.append(
                HighlightPayload(
                    label=m.canonical_label_ar,
                    canonical_label=m.canonical_label_ar,
                    span_text=raw_span_text,
                    start=raw_start,
                    end=raw_end,
                    severity=_esi_to_severity(m.esi),
                    negated=False,
                    uncertain=False,
                    current=True,
                    source="fast",
                )
            )
            anchored = True
            break  # one span per match is enough for the UI
        if not anchored and m.spans:
            logger.debug(
                "fast_decision: dropped highlight for %s (no token-aligned anchor)",
                m.canonical_label_ar,
            )
    return _dedupe_highlights(payloads)


def _dedupe_highlights(
    highlights: list[HighlightPayload],
) -> list[HighlightPayload]:
    seen: set[tuple[int, int, str]] = set()
    out: list[HighlightPayload] = []
    for item in highlights:
        key = (item.start or -1, item.end or -1, item.canonical_label)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _esi_to_severity(esi: int) -> str:
    if esi <= 2:
        return "high"
    if esi == 3:
        return "medium"
    return "low"


def matches_to_keyword_labels(matches: list[TriageMatch]) -> list[str]:
    """UI keyword chips come from the verbatim transcript span text
    (not the canonical label), so the chip text equals the highlight
    text. This is the invariant the dispatcher UX repeatedly wanted.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        if m.negated:
            continue
        for span in m.spans:
            text = (span.text or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
                break
    return out


# ── Entrypoint ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FastDecisionResult:
    """Return shape of ``FastDecisionService.process``."""

    revision: int
    fast_triage: TriageFastResult
    chunk_matches: list[TriageMatch]
    highlights: list[HighlightPayload]
    keyword_labels: list[str]
    location: PatientLocation | None
    patient_count: int | None
    demographics: PatientDemographics = PatientDemographics()


class FastDecisionService:
    """All deterministic fast-path work for the live pipeline."""

    def __init__(self) -> None:
        # The triage engine holds the keyword bank + rapidfuzz indices;
        # we reuse its singleton instead of rebuilding them here.
        self._engine = get_triage_engine()

    def process(
        self,
        *,
        case_id: int,
        revision: int,
        chunk_text: str,
        transcript_text: str,
        provisional: bool = False,
    ) -> FastDecisionResult:
        """Run the fast decisions for a single chunk.

        ``revision`` is the transcript revision this chunk produced
        (from ``TranscriptService.ingest_chunk``). It is attached to
        the result so ``case_state_service`` can drop stale arrivals.
        """
        chunk_text = (chunk_text or "").strip()
        transcript_text = (transcript_text or "").strip()

        with stage_timer(None, "fast_decision", case_id=case_id, revision=revision):
            if provisional:
                fast_event = self._engine.evaluate_preview(
                    case_id,
                    chunk_text,
                    preview_transcript=transcript_text,
                )
            else:
                fast_event = self._engine.evaluate_chunk(case_id, chunk_text)

            triage_result = fast_event.result
            chunk_matches = list(fast_event.chunk_matches)
            # Highlights are grounded against the MERGED transcript,
            # not the chunk, so the UI never paints offsets that fall
            # outside the text it is actually rendering.
            highlights = ground_highlights_from_matches(
                transcript_text=transcript_text,
                matches=list(triage_result.matches),
            )
            keyword_labels = matches_to_keyword_labels(
                list(triage_result.matches)
            )
            location = extract_grounded_location(transcript_text)
            patient_count = infer_patient_count(transcript_text)
            demographics = extract_patient_demographics(transcript_text)

        return FastDecisionResult(
            revision=revision,
            fast_triage=triage_result,
            chunk_matches=chunk_matches,
            highlights=highlights,
            keyword_labels=keyword_labels,
            location=location,
            patient_count=patient_count,
            demographics=demographics,
        )


_service: FastDecisionService | None = None


def get_fast_decision_service() -> FastDecisionService:
    global _service
    if _service is None:
        _service = FastDecisionService()
    return _service
