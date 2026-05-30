"""
Per-case accumulated evidence state.

Every bubble that closes runs the matcher and produces a fresh set of
matches. We keep those matches in a rolling per-case store so that the
dispatcher sees a cumulative picture across the whole call, not just the
last bubble.

Rules:

  * A concept seen in a new bubble wins over an older, weaker hit of the
    same concept (keeps the best keyword + merges span lists, deduped).
  * Evidence is NOT expired by a wall-clock TTL. Once a concept is seen
    it keeps counting until a newer (e.g. LLM) result overrides it; the
    decision of whether a match still applies is owned by
    ``case_state_service``, not by a timer here. See ``active_matches``.
  * Concepts whose cumulative confidence falls below ``min_active_confidence``
    are excluded from the triage decision but kept for evidence display.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from backend.triage_engine.keyword_bank import KeywordBank
from backend.triage_engine.models import (
    TriageEvidenceSpan,
    TriageMatch,
    TriageRiskModifier,
)


@dataclass
class CaseEvidenceConfig:
    evidence_ttl_seconds: float = 90.0
    min_active_confidence: float = 0.5


def _config_from_bank(bank: KeywordBank) -> CaseEvidenceConfig:
    cfg = bank.pipeline.get("case_evidence", {})
    return CaseEvidenceConfig(
        evidence_ttl_seconds=float(cfg.get("evidence_ttl_seconds", 90)),
        min_active_confidence=float(cfg.get("min_active_confidence", 0.5)),
    )


@dataclass
class CaseEvidence:
    """Mutable per-case state."""

    case_id: int
    config: CaseEvidenceConfig
    matches_by_concept: dict[str, TriageMatch] = field(default_factory=dict)
    modifiers_by_id: dict[str, TriageRiskModifier] = field(default_factory=dict)
    created_at: float = field(default_factory=time.perf_counter)
    last_chunk_at: float = 0.0
    chunk_count: int = 0
    full_transcript: str = ""

    def ingest(
        self,
        new_matches: Iterable[TriageMatch],
        new_modifiers: Iterable[TriageRiskModifier],
        *,
        chunk_text: str,
    ) -> None:
        self.last_chunk_at = time.perf_counter()
        self.chunk_count += 1
        if chunk_text:
            if self.full_transcript:
                self.full_transcript = f"{self.full_transcript} {chunk_text}".strip()
            else:
                self.full_transcript = chunk_text.strip()

        for m in new_matches:
            existing = self.matches_by_concept.get(m.concept_id)
            if existing is None:
                self.matches_by_concept[m.concept_id] = m.model_copy(deep=True)
                continue
            # Merge: keep the highest fuzzy score and the union of spans.
            # Negation is a per-occurrence signal, so the newest hit's
            # negation flag wins rather than being OR-ed across hits.
            if m.fuzzy_score > existing.fuzzy_score:
                existing.matched_keyword = m.matched_keyword
                existing.matched_dialect = m.matched_dialect
                existing.fuzzy_score = m.fuzzy_score
                existing.is_fuzzy = m.is_fuzzy
            existing.negated = m.negated
            existing.confidence = max(existing.confidence, m.confidence)
            existing.last_seen_at = m.last_seen_at or time.perf_counter()
            for span in m.spans:
                if not any(
                    s.start == span.start and s.end == span.end and s.text == span.text
                    for s in existing.spans
                ):
                    existing.spans.append(span)

        for mod in new_modifiers:
            # Modifiers are boolean signals — newest wins.
            self.modifiers_by_id[mod.modifier_id] = mod.model_copy(deep=True)

    def active_matches(self) -> list[TriageMatch]:
        """Return the set of matches that count for triage.

        IMPORTANT: evidence is NOT decayed by TTL anymore. The
        previous behaviour silently downgraded a cardiac-arrest match
        ~90 seconds into the call because the timer had expired, which
        is exactly the "red dropped to green" regression the dispatcher
        reported. Whether a match still counts is now a merge-policy
        decision owned by ``case_state_service`` (which can be told by
        a newer enriched LLM run that the severity has resolved), not
        a wall-clock decision here.

        We still drop matches below ``min_active_confidence`` to
        filter pure noise hits; that threshold is a property of the
        matcher, not of time.
        """
        min_conf = self.config.min_active_confidence
        out: list[TriageMatch] = []
        for m in self.matches_by_concept.values():
            if m.confidence < min_conf:
                continue
            out.append(m)
        return out

    def all_matches(self) -> list[TriageMatch]:
        return list(self.matches_by_concept.values())

    def all_modifiers(self) -> list[TriageRiskModifier]:
        return list(self.modifiers_by_id.values())

    def reset(self) -> None:
        self.matches_by_concept.clear()
        self.modifiers_by_id.clear()
        self.full_transcript = ""
        self.chunk_count = 0
        self.last_chunk_at = 0.0


class CaseEvidenceStore:
    """Process-wide thread-safe registry of per-case evidence."""

    def __init__(self, bank: KeywordBank) -> None:
        self._bank = bank
        self._config = _config_from_bank(bank)
        self._cases: dict[int, CaseEvidence] = {}
        self._lock = threading.Lock()

    def get(self, case_id: int) -> CaseEvidence:
        with self._lock:
            existing = self._cases.get(case_id)
            if existing is None:
                existing = CaseEvidence(case_id=case_id, config=self._config)
                self._cases[case_id] = existing
            return existing

    def reset(self, case_id: int) -> None:
        with self._lock:
            existing = self._cases.get(case_id)
            if existing is not None:
                existing.reset()

    def drop(self, case_id: int) -> None:
        with self._lock:
            self._cases.pop(case_id, None)

    def active_case_ids(self) -> list[int]:
        with self._lock:
            return list(self._cases.keys())
