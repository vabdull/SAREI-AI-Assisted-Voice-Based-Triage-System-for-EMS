"""
Loads the YAML keyword bank into a flat, normalized, in-memory index.

The matcher never looks at the raw YAML — it only queries :class:`KeywordBank`
which pre-normalizes every phrase at load time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal

import yaml

from backend.triage_engine.normalization import normalize

logger = logging.getLogger(__name__)


Dialect = Literal["msa", "najdi", "hijazi", "khaleeji", "universal"]
_DIALECTS: tuple[Dialect, ...] = ("msa", "najdi", "hijazi", "khaleeji", "universal")


# Defaults applied if pipeline.yaml is missing. Any change to these should
# also be reflected in configs/triage/pipeline.yaml.
_DEFAULT_PIPELINE: dict = {
    "matcher": {
        "fuzzy_threshold": 82,
        "top_k_per_chunk": 20,
        "min_chunk_chars": 2,
        "max_candidates": 2000,
    },
    "negation": {
        "window_tokens": 3,
    },
    "case_evidence": {
        "evidence_ttl_seconds": 90,
        "min_active_confidence": 0.5,
    },
    "rules": {
        "esi_to_level": {1: "red", 2: "red", 3: "yellow", 4: "green", 5: "green"},
        "default_esi": 5,
    },
    "llm_enricher": {
        "silence_gap_seconds": 2.5,
        "silence_gap_seconds_red": 1.2,
        "llm_timeout_seconds": 30.0,
        "max_concurrent_jobs": 4,
    },
    "websocket": {
        "heartbeat_seconds": 20,
        "client_queue_size": 64,
    },
}


@dataclass(frozen=True)
class SymptomEntry:
    """A single symptom / concept from the keyword bank, fully normalized."""

    concept_id: str
    category: str
    esi: int
    weight: int
    canonical_label_ar: str
    description_ar: str
    phrases_by_dialect: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class RiskModifierEntry:
    modifier_id: str
    note_ar: str
    escalate: bool
    triggers_normalized: tuple[str, ...]
    triggers_raw: tuple[str, ...]


@dataclass
class KeywordIndexEntry:
    """A single normalized phrase paired with the symptom it belongs to."""

    normalized_phrase: str
    raw_phrase: str
    dialect: Dialect
    symptom: SymptomEntry


@dataclass
class KeywordBank:
    """
    Fully loaded, normalized keyword bank.

    Public surface:
      - :attr:`symptoms` / :attr:`symptoms_by_id`
      - :attr:`phrase_index` — list of :class:`KeywordIndexEntry`; the matcher
        uses this directly. Deduplicated on (normalized_phrase, concept_id).
      - :attr:`risk_modifiers` — list of :class:`RiskModifierEntry`.
      - :attr:`negation_particles` — normalized forms of negation tokens.
      - :attr:`negation_window_tokens` — how many preceding tokens to scan.
    """

    symptoms: tuple[SymptomEntry, ...]
    symptoms_by_id: dict[str, SymptomEntry]
    phrase_index: tuple[KeywordIndexEntry, ...]
    risk_modifiers: tuple[RiskModifierEntry, ...]
    negation_particles: tuple[str, ...]
    negation_window_tokens: int
    pipeline: dict

    def phrases_normalized(self) -> list[str]:
        """Flat list of normalized phrases, deduplicated, in index order."""
        return [e.normalized_phrase for e in self.phrase_index]


def _repo_root() -> Path:
    # backend/triage_engine/keyword_bank.py -> repo root
    return Path(__file__).resolve().parents[2]


def _default_bank_path() -> Path:
    return _repo_root() / "configs" / "triage" / "keyword_bank.yaml"


def _default_pipeline_path() -> Path:
    return _repo_root() / "configs" / "triage" / "pipeline.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Keyword bank YAML not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Keyword bank YAML must be a mapping: {path}")
    return data


def _load_pipeline(path: Path) -> dict:
    if not path.is_file():
        logger.warning("pipeline.yaml not found at %s - using defaults", path)
        return _DEFAULT_PIPELINE
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        logger.warning("pipeline.yaml is not a mapping - using defaults")
        return _DEFAULT_PIPELINE
    merged: dict = {k: dict(v) if isinstance(v, dict) else v for k, v in _DEFAULT_PIPELINE.items()}
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def _extract_symptom(raw: dict) -> SymptomEntry | None:
    if not isinstance(raw, dict):
        return None
    concept_id = str(raw.get("id") or "").strip()
    if not concept_id:
        return None
    category = str(raw.get("category") or "misc").strip() or "misc"
    try:
        esi = int(raw.get("esi", 5))
    except (TypeError, ValueError):
        esi = 5
    if esi < 1 or esi > 5:
        esi = 5
    try:
        weight = int(raw.get("weight", 1))
    except (TypeError, ValueError):
        weight = 1
    weight = max(0, min(10, weight))

    description = str(raw.get("description_ar") or "").strip()
    canonical = description or concept_id.replace("_", " ")

    # Build the dialect → normalized tuple map.
    phrases_map_raw = raw.get("phrases")
    if isinstance(phrases_map_raw, dict):
        phrases_src = phrases_map_raw
    else:
        # Legacy shape: flat "msa"/"najdi"/etc keys on the symptom itself.
        phrases_src = {d: raw.get(d) for d in _DIALECTS}

    phrases_by_dialect: dict[str, tuple[str, ...]] = {}
    for dialect in _DIALECTS:
        vals = phrases_src.get(dialect) or []
        if not isinstance(vals, list):
            continue
        cleaned: list[str] = []
        seen: set[str] = set()
        for v in vals:
            if not isinstance(v, str):
                continue
            n = normalize(v)
            if not n or n in seen:
                continue
            seen.add(n)
            cleaned.append(n)
        if cleaned:
            phrases_by_dialect[dialect] = tuple(cleaned)

    if not phrases_by_dialect:
        logger.warning("Symptom %s has no phrases after normalization", concept_id)
        return None

    return SymptomEntry(
        concept_id=concept_id,
        category=category,
        esi=esi,
        weight=weight,
        canonical_label_ar=canonical,
        description_ar=description,
        phrases_by_dialect=phrases_by_dialect,
    )


def _extract_modifier(raw: dict) -> RiskModifierEntry | None:
    if not isinstance(raw, dict):
        return None
    modifier_id = str(raw.get("id") or "").strip()
    if not modifier_id:
        return None
    note = str(raw.get("note_ar") or raw.get("note") or modifier_id)
    escalate = bool(raw.get("escalate", False))
    triggers_raw_list = raw.get("triggers") or []
    if not isinstance(triggers_raw_list, list):
        return None
    raw_triggers: list[str] = []
    norm_triggers: list[str] = []
    seen: set[str] = set()
    for t in triggers_raw_list:
        if not isinstance(t, str):
            continue
        n = normalize(t)
        if not n or n in seen:
            continue
        seen.add(n)
        raw_triggers.append(t)
        norm_triggers.append(n)
    if not norm_triggers:
        return None
    return RiskModifierEntry(
        modifier_id=modifier_id,
        note_ar=note,
        escalate=escalate,
        triggers_normalized=tuple(norm_triggers),
        triggers_raw=tuple(raw_triggers),
    )


def _build_phrase_index(symptoms: Iterable[SymptomEntry]) -> list[KeywordIndexEntry]:
    seen: set[tuple[str, str]] = set()
    entries: list[KeywordIndexEntry] = []
    for sym in symptoms:
        for dialect, phrases in sym.phrases_by_dialect.items():
            for norm_phrase in phrases:
                key = (norm_phrase, sym.concept_id)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    KeywordIndexEntry(
                        normalized_phrase=norm_phrase,
                        raw_phrase=norm_phrase,  # stored normalized; matcher works on this
                        dialect=dialect,  # type: ignore[arg-type]
                        symptom=sym,
                    )
                )
    return entries


def load_keyword_bank(
    bank_path: str | Path | None = None,
    pipeline_path: str | Path | None = None,
) -> KeywordBank:
    """Read + normalize + index the keyword bank from YAML."""
    bank_path = Path(bank_path) if bank_path else _default_bank_path()
    pipeline_path = Path(pipeline_path) if pipeline_path else _default_pipeline_path()

    data = _load_yaml(bank_path)
    pipeline = _load_pipeline(pipeline_path)

    raw_symptoms = data.get("symptoms") or []
    if not isinstance(raw_symptoms, list):
        raise ValueError("'symptoms' in keyword_bank.yaml must be a list")

    symptoms: list[SymptomEntry] = []
    symptoms_by_id: dict[str, SymptomEntry] = {}
    for raw in raw_symptoms:
        sym = _extract_symptom(raw)
        if sym is None:
            continue
        if sym.concept_id in symptoms_by_id:
            logger.warning("Duplicate symptom id %s - keeping first", sym.concept_id)
            continue
        symptoms.append(sym)
        symptoms_by_id[sym.concept_id] = sym

    raw_modifiers = data.get("risk_modifiers") or []
    if not isinstance(raw_modifiers, list):
        raw_modifiers = []
    modifiers: list[RiskModifierEntry] = []
    for raw in raw_modifiers:
        mod = _extract_modifier(raw)
        if mod is not None:
            modifiers.append(mod)

    raw_negation = data.get("negation") or {}
    particles_src = raw_negation.get("particles") if isinstance(raw_negation, dict) else None
    if not isinstance(particles_src, list):
        particles_src = []

    # Pipeline.yaml may add dialect-specific particles ("particles_extra")
    # so dispatch can extend coverage without touching the keyword
    # bank. We merge them here, normalized, and de-duplicated.
    pipeline_negation = pipeline.get("negation", {}) if isinstance(pipeline, dict) else {}
    particles_extra = pipeline_negation.get("particles_extra") or []
    if not isinstance(particles_extra, list):
        particles_extra = []

    negation_particles: list[str] = []
    seen_part: set[str] = set()
    for p in list(particles_src) + list(particles_extra):
        if not isinstance(p, str):
            continue
        n = normalize(p)
        if n and n not in seen_part:
            seen_part.add(n)
            negation_particles.append(n)

    negation_window = int(
        pipeline.get("negation", {}).get("window_tokens")
        or (raw_negation.get("window_tokens") if isinstance(raw_negation, dict) else None)
        or 3
    )

    phrase_index = _build_phrase_index(symptoms)

    logger.info(
        "Keyword bank loaded: %d symptoms, %d phrases, %d risk modifiers, %d negation particles",
        len(symptoms),
        len(phrase_index),
        len(modifiers),
        len(negation_particles),
    )

    return KeywordBank(
        symptoms=tuple(symptoms),
        symptoms_by_id=symptoms_by_id,
        phrase_index=tuple(phrase_index),
        risk_modifiers=tuple(modifiers),
        negation_particles=tuple(negation_particles),
        negation_window_tokens=negation_window,
        pipeline=pipeline,
    )


@lru_cache(maxsize=1)
def get_keyword_bank() -> KeywordBank:
    """Process-wide cached keyword bank. Call :func:`reload_keyword_bank` to refresh."""
    return load_keyword_bank()


def reload_keyword_bank() -> KeywordBank:
    """Force a fresh load (useful for tests / hot-reload in dev)."""
    get_keyword_bank.cache_clear()
    return get_keyword_bank()
