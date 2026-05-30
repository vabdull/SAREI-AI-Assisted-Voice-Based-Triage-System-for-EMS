"""
Layer 1 — Fuzzy matcher.

Scans a single chunk of normalized Arabic text against the keyword bank
and returns every matched symptom with character-level evidence spans.

Matching happens in two passes:

  1. **Token-aware exact match.** The chunk is tokenized on Arabic /
     ASCII word boundaries. Single-token bank phrases match a token
     whose normalized form equals the phrase (optionally after a single
     conservative clitic-prefix strip, e.g. "والصداع" → "صداع"). Multi-
     token bank phrases must match a consecutive run of transcript
     tokens. This pass emits score=100 and was the historical
     "Pass 1 = norm.find(phrase)" — which silently produced false
     positives like ``سم`` inside ``اسمي``. The new pass is genuinely
     boundary-aware.

  2. **Fuzzy partial-ratio alignment.** For phrases that didn't
     exact-match, rapidfuzz ``partial_ratio_alignment`` runs against
     the normalized text. The alignment window is then **snapped to
     token boundaries** before being accepted, so a fuzzy hit can
     never paint a partial-token slice. Content-word coverage and the
     minimum-phrase-length guard from the old fuzzy pass are kept.

The second pass is skipped for any concept that already produced a
100% exact match in pass 1, so duplicate work is avoided.

All spans are in the **normalized** coordinate system of the chunk.
The grounding layer (``fast_decision_service``) translates them into
raw transcript offsets via ``normalize_with_map`` / the token-aware
finder — the frontend never re-anchors.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from rapidfuzz import fuzz

from backend.triage_engine.keyword_bank import (
    KeywordBank,
    KeywordIndexEntry,
    SymptomEntry,
)


# Arabic function words + very short tokens that must NOT on their own be
# enough to justify a fuzzy match. A partial_ratio alignment that hits only
# on words like "ما" or "شديد" is a classic false positive because those
# appear in many unrelated phrases. We require at least one "content word"
# from the keyword phrase to also appear in the aligned span.
_FUNCTION_WORDS = frozenset({
    "ما", "لا", "في", "من", "الى", "على", "عن", "مع", "و", "او",
    "ثم", "هو", "هي", "انا", "انت", "هذا", "هذه", "ذلك", "كل", "بعد",
    "قبل", "عند", "الى", "ب", "ل", "ال", "يا", "يلا", "مش", "مو",
    "انه", "اني", "ماهو", "ما هو", "شديد", "قوي", "كثير", "كتير", "وايد",
    "جدا", "مره", "جنب", "فوق", "تحت", "وراء", "امام", "يوقف",
    "توقف", "يقدر", "تقدر", "يقدرو", "تقدري", "كان", "بتوقف",
})

# When checking whether a fuzzy match is supported by real content words in
# the text, we accept a per-token ratio >= this value as "close enough"
# (handles Arabic morphology: "وجهه" vs "وجهها", "يحرك" vs "تحرك", ...).
_CONTENT_WORD_TOKEN_RATIO = 82.0


def _content_words(phrase: str) -> tuple[str, ...]:
    """
    Return the non-function, non-trivial tokens of a normalized phrase.
    Used by the fuzzy-match filter. A keyword phrase with NO content words
    (e.g. ``"ما يوقف"``) falls back to exact-match only — it's too generic
    to fuzzy on.
    """
    out: list[str] = []
    for tok in phrase.split():
        if len(tok) < 3:
            continue
        if tok in _FUNCTION_WORDS:
            continue
        out.append(tok)
    return tuple(out)


def _required_content_coverage(n_content_words: int) -> int:
    """
    Minimum number of keyword content words that must have a fuzzy match
    in the alignment window. For short phrases we require near-full
    coverage; the threshold loosens for longer, more specific phrases.
    """
    if n_content_words <= 1:
        return 1
    if n_content_words <= 3:
        return n_content_words  # 100%
    # 4+ content words: allow missing ~1/4 (covers paraphrasing).
    return max(3, int(round(n_content_words * 0.75)))
from backend.triage_engine.models import (
    Dialect,
    TriageEvidenceSpan,
    TriageMatch,
    TriageRiskModifier,
)
from backend.triage_engine.negation import is_negated
from backend.triage_engine.normalization import (
    is_arabic_word_char,
    normalize,
    strip_clitic_prefix,
    tokenize_with_offsets,
)

logger = logging.getLogger(__name__)


@dataclass
class MatcherConfig:
    fuzzy_threshold: float = 82.0
    min_chunk_chars: int = 2
    max_candidates: int = 2000
    # Token-aware Pass 1 settings:
    # Whether to tolerate a single conservative clitic prefix on the
    # FIRST transcript token of a candidate match window (و / ف / ب /
    # ك / ل / ال and combinations). The phrase itself is never
    # stripped. Conservative by default — see normalization.py for the
    # safety rails.
    allow_clitic_prefix: bool = True
    # Minimum stem length after clitic stripping. ``4`` is what
    # prevents "وسم" → "سم" collapsing — the stem ("سم") is too short
    # so the stripping is rejected and "وسم" is treated literally.
    clitic_min_stem_chars: int = 4
    # Minimum LENGTH (in characters) of a bank phrase for it to take
    # part in the fuzzy fallback pass. Short phrases like "سم" cannot
    # be safely fuzzy-matched on the full chunk; they must come from
    # the token-aware exact pass only.
    fuzzy_min_phrase_chars: int = 3


class FuzzyMatcher:
    """Layer 1 engine. Build once, reuse across chunks."""

    def __init__(self, bank: KeywordBank, config: MatcherConfig | None = None) -> None:
        self.bank = bank
        if config is None:
            matcher_cfg = bank.pipeline.get("matcher", {})
            config = MatcherConfig(
                fuzzy_threshold=float(matcher_cfg.get("fuzzy_threshold", 82)),
                min_chunk_chars=int(matcher_cfg.get("min_chunk_chars", 2)),
                max_candidates=int(matcher_cfg.get("max_candidates", 2000)),
                allow_clitic_prefix=bool(
                    matcher_cfg.get("allow_clitic_prefix", True)
                ),
                clitic_min_stem_chars=int(
                    matcher_cfg.get("clitic_min_stem_chars", 4)
                ),
                fuzzy_min_phrase_chars=int(
                    matcher_cfg.get("fuzzy_min_phrase_chars", 3)
                ),
            )
        self.config = config

        # Pre-sort phrases longer-first so "قلبه وقف" beats "قلبه" on exact-pass
        # score comparisons for the same concept. The concept-dedupe logic
        # already handles this but longer phrases are more specific signals.
        self._phrases: tuple[KeywordIndexEntry, ...] = tuple(
            sorted(
                bank.phrase_index[: config.max_candidates],
                key=lambda e: -len(e.normalized_phrase),
            )
        )
        # Pre-compute the content-word set for each phrase so the fuzzy
        # post-filter doesn't redo the work per bubble.
        self._content_words_by_phrase: dict[str, tuple[str, ...]] = {
            e.normalized_phrase: _content_words(e.normalized_phrase)
            for e in self._phrases
        }
        # Pre-tokenize each bank phrase once. The matcher does the
        # actual scanning against tokens of the chunk, not via raw
        # substring ``find``. Empty phrases are filtered out
        # defensively; they should never reach here from a sane bank.
        self._phrase_tokens: dict[str, tuple[str, ...]] = {
            e.normalized_phrase: tuple(
                t for t in e.normalized_phrase.split(" ") if t
            )
            for e in self._phrases
        }

    # ── Public API ────────────────────────────────────────────────────────

    def match(self, chunk: str) -> tuple[list[TriageMatch], list[TriageRiskModifier], str]:
        """
        Match ``chunk`` against the keyword bank.

        Returns:
            matches  — list of :class:`TriageMatch` keyed by concept_id
            modifiers — list of :class:`TriageRiskModifier` found in ``chunk``
            normalized_chunk — the chunk after normalization (for debugging)
        """
        norm = normalize(chunk)
        if len(norm) < self.config.min_chunk_chars:
            return [], [], norm

        t0 = time.perf_counter()
        matches_by_concept: dict[str, TriageMatch] = {}

        # Tokenize the normalized chunk once. We work in normalized
        # coordinates here so the chunk text == norm; the ``raw_*``
        # fields of each token therefore equal ``norm_*`` (the offset
        # map is the identity for an already-normalized input). The
        # grounding layer re-applies tokenization on the full raw
        # transcript when it converts these spans to raw offsets.
        _norm_chunk, _offset_map, chunk_tokens = tokenize_with_offsets(norm)
        chunk_token_texts = [t.text for t in chunk_tokens]
        n_chunk_tokens = len(chunk_tokens)

        # Pass 1 — token-aware exact match.
        for entry in self._phrases:
            phrase_tokens = self._phrase_tokens.get(entry.normalized_phrase, ())
            if not phrase_tokens:
                continue
            n_phrase = len(phrase_tokens)
            if n_phrase > n_chunk_tokens:
                continue
            hit = self._find_token_aligned_match(
                chunk_tokens=chunk_tokens,
                chunk_token_texts=chunk_token_texts,
                phrase_tokens=phrase_tokens,
            )
            if hit is None:
                continue
            t_start_idx, t_end_idx = hit
            norm_start = chunk_tokens[t_start_idx].norm_start
            norm_end = chunk_tokens[t_end_idx - 1].norm_end
            span = TriageEvidenceSpan(
                start=norm_start,
                end=norm_end,
                text=norm[norm_start:norm_end],
            )
            self._ingest(
                matches_by_concept,
                entry=entry,
                span=span,
                fuzzy_score=100.0,
                is_fuzzy=False,
                normalized_text=norm,
            )

        # Pass 2 — fuzzy alignment only for concepts not yet exact-matched.
        # The alignment window is SNAPPED to token boundaries after the
        # fact so a fuzzy hit can never paint a partial-token slice
        # (the historical "سم inside اسمي" failure mode).
        for entry in self._phrases:
            if entry.symptom.concept_id in matches_by_concept and (
                matches_by_concept[entry.symptom.concept_id].fuzzy_score >= 100.0
            ):
                continue
            if len(entry.normalized_phrase) < self.config.fuzzy_min_phrase_chars:
                continue
            try:
                alignment = fuzz.partial_ratio_alignment(
                    entry.normalized_phrase,
                    norm,
                    score_cutoff=self.config.fuzzy_threshold,
                )
            except Exception:  # pragma: no cover - rapidfuzz edge case
                alignment = None
            if alignment is None:
                continue
            score = float(alignment.score)
            if score < self.config.fuzzy_threshold:
                continue
            dest_start = max(0, int(alignment.dest_start))
            dest_end = min(len(norm), int(alignment.dest_end))
            if dest_end <= dest_start:
                continue

            # Snap to token boundaries — drop the match entirely if the
            # alignment doesn't cover whole tokens. This is what makes
            # the fuzzy fallback boundary-correct.
            snapped = self._snap_to_token_bounds(
                chunk_tokens=chunk_tokens, start=dest_start, end=dest_end
            )
            if snapped is None:
                continue
            dest_start, dest_end = snapped
            span_text = norm[dest_start:dest_end]

            # Content-word coverage guard. For a fuzzy match to count we
            # require at least ceil(3/4) of the keyword's meaningful tokens
            # to have a close-enough token-level match somewhere in the
            # alignment window (widened a bit around the edges to tolerate
            # partial-ratio alignment truncation). This is what stops
            # "ربو وما يتنفس" from matching "ما يتنفس" on its own.
            content = self._content_words_by_phrase.get(entry.normalized_phrase, ())
            if not content:
                # Phrase has no content words at all -> too generic to fuzzy.
                continue
            # Widen the window by up to 3 chars on each side to catch
            # tokens that rapidfuzz may have clipped.
            window_start = max(0, dest_start - 3)
            window_end = min(len(norm), dest_end + 3)
            window_text = norm[window_start:window_end]
            window_tokens = [t for t in window_text.split() if t]
            if not window_tokens:
                continue
            covered = 0
            for cw in content:
                for wt in window_tokens:
                    if cw == wt or cw in wt or wt in cw:
                        covered += 1
                        break
                    if fuzz.ratio(cw, wt) >= _CONTENT_WORD_TOKEN_RATIO:
                        covered += 1
                        break
            if covered < _required_content_coverage(len(content)):
                continue

            span = TriageEvidenceSpan(
                start=dest_start,
                end=dest_end,
                text=span_text,
            )
            self._ingest(
                matches_by_concept,
                entry=entry,
                span=span,
                fuzzy_score=score,
                is_fuzzy=True,
                normalized_text=norm,
            )

        # Negation pass — applied over the final per-concept best match.
        for m in matches_by_concept.values():
            if m.spans and is_negated(
                norm,
                m.spans[0].start,
                self.bank.negation_particles,
                self.bank.negation_window_tokens,
            ):
                m.negated = True

        # Risk modifiers (exact only — they are short deterministic tokens).
        modifiers = self._detect_modifiers(norm)

        took_ms = (time.perf_counter() - t0) * 1000.0
        if took_ms > 50:
            logger.warning("Matcher slow: %.1fms chunk_len=%d", took_ms, len(norm))

        return list(matches_by_concept.values()), modifiers, norm

    # ── Internals ─────────────────────────────────────────────────────────

    def _find_token_aligned_match(
        self,
        *,
        chunk_tokens: list,
        chunk_token_texts: list[str],
        phrase_tokens: tuple[str, ...],
    ) -> tuple[int, int] | None:
        """Return ``(start_idx, end_idx)`` of a token-aligned match.

        Indices are into ``chunk_tokens`` and form a half-open range.
        Returns ``None`` when the phrase doesn't appear at any token-
        aligned position.

        Match policy:

        * Single-token phrase: scan tokens; the leading transcript
          token may carry a clitic prefix (و/ف/ب/ك/ل/ال and combos)
          and still match if stripping yields the phrase.
        * Multi-token phrase: a consecutive run of transcript tokens
          must match the phrase. Only the FIRST transcript token of
          the candidate window is allowed to carry a clitic prefix,
          mirroring Arabic morphology — proclitics attach to the head
          of the noun phrase.

        We return the LAST occurrence (rightmost) so that a transcript
        repeating a symptom highlights the freshest mention. Triage
        decisions don't care which span we choose because the
        per-concept dedupe collapses them anyway.
        """
        n_phrase = len(phrase_tokens)
        if n_phrase == 0:
            return None
        n_chunk = len(chunk_tokens)
        if n_phrase > n_chunk:
            return None
        last_hit: tuple[int, int] | None = None
        for start in range(n_chunk - n_phrase + 1):
            head = chunk_token_texts[start]
            if head != phrase_tokens[0]:
                if not self.config.allow_clitic_prefix:
                    continue
                stripped = strip_clitic_prefix(
                    head, min_stem_chars=self.config.clitic_min_stem_chars
                )
                if stripped != phrase_tokens[0]:
                    continue
            ok = True
            for k in range(1, n_phrase):
                if chunk_token_texts[start + k] != phrase_tokens[k]:
                    ok = False
                    break
            if not ok:
                continue
            last_hit = (start, start + n_phrase)
        return last_hit

    @staticmethod
    def _snap_to_token_bounds(
        *,
        chunk_tokens: list,
        start: int,
        end: int,
    ) -> tuple[int, int] | None:
        """Snap a normalized-string range to whole-token boundaries.

        Returns ``None`` if the requested range doesn't intersect any
        token at all. Otherwise grows the range outward to fully cover
        every token it touches, so partial-token slices never reach
        the UI as a highlight span.
        """
        if not chunk_tokens or end <= start:
            return None
        new_start: int | None = None
        new_end: int | None = None
        for tok in chunk_tokens:
            if tok.norm_end <= start:
                continue
            if tok.norm_start >= end:
                break
            new_start = tok.norm_start if new_start is None else min(new_start, tok.norm_start)
            new_end = tok.norm_end if new_end is None else max(new_end, tok.norm_end)
        if new_start is None or new_end is None:
            return None
        if new_end <= new_start:
            return None
        return new_start, new_end

    def _ingest(
        self,
        matches_by_concept: dict[str, TriageMatch],
        *,
        entry: KeywordIndexEntry,
        span: TriageEvidenceSpan,
        fuzzy_score: float,
        is_fuzzy: bool,
        normalized_text: str,
    ) -> None:
        sym = entry.symptom
        existing = matches_by_concept.get(sym.concept_id)
        if existing is None:
            confidence = _score_to_confidence(fuzzy_score, sym.weight)
            matches_by_concept[sym.concept_id] = TriageMatch(
                concept_id=sym.concept_id,
                category=sym.category,
                esi=sym.esi,
                weight=sym.weight,
                canonical_label_ar=sym.canonical_label_ar,
                matched_keyword=entry.normalized_phrase,
                matched_dialect=entry.dialect,  # type: ignore[arg-type]
                fuzzy_score=fuzzy_score,
                is_fuzzy=is_fuzzy,
                negated=False,
                confidence=confidence,
                spans=[span],
                last_seen_at=time.perf_counter(),
            )
            return

        # Existing match: merge. Keep the best-scoring keyword + both spans.
        if fuzzy_score > existing.fuzzy_score:
            existing.matched_keyword = entry.normalized_phrase
            existing.matched_dialect = entry.dialect  # type: ignore[assignment]
            existing.fuzzy_score = fuzzy_score
            existing.is_fuzzy = is_fuzzy
            existing.confidence = max(
                existing.confidence, _score_to_confidence(fuzzy_score, entry.symptom.weight)
            )
        # Dedupe spans on (start, end).
        if not any(s.start == span.start and s.end == span.end for s in existing.spans):
            existing.spans.append(span)

    def _detect_modifiers(self, norm_text: str) -> list[TriageRiskModifier]:
        results: list[TriageRiskModifier] = []
        for mod in self.bank.risk_modifiers:
            for raw_trigger, norm_trigger in zip(mod.triggers_raw, mod.triggers_normalized):
                if not norm_trigger:
                    continue
                # Use a boundary-aware sweep rather than ``find``. A
                # 3-char modifier like "حاد" must not match inside
                # "حادث". We scan all occurrences and accept the first
                # one that aligns with non-letter neighbours.
                pos = self._find_with_boundaries(norm_text, norm_trigger)
                if pos < 0:
                    continue
                span = TriageEvidenceSpan(
                    start=pos,
                    end=pos + len(norm_trigger),
                    text=norm_trigger,
                )
                results.append(
                    TriageRiskModifier(
                        modifier_id=mod.modifier_id,
                        note_ar=mod.note_ar,
                        escalate=mod.escalate,
                        trigger=raw_trigger,
                        spans=[span],
                    )
                )
                break  # one trigger per modifier is enough
        return results

    @staticmethod
    def _find_with_boundaries(haystack: str, needle: str) -> int:
        """Return the first occurrence of ``needle`` in ``haystack``
        that is flanked by non-word characters on both sides (Arabic
        word boundary). Returns ``-1`` if no boundary-clean match
        exists. Used by the modifier sweep to mirror the matcher's
        new boundary policy.
        """
        if not haystack or not needle:
            return -1
        start = 0
        n_h = len(haystack)
        n_n = len(needle)
        while start <= n_h - n_n:
            idx = haystack.find(needle, start)
            if idx < 0:
                return -1
            left_ok = (idx == 0) or not is_arabic_word_char(haystack[idx - 1])
            right_pos = idx + n_n
            right_ok = (right_pos >= n_h) or not is_arabic_word_char(haystack[right_pos])
            if left_ok and right_ok:
                return idx
            start = idx + 1
        return -1


def _score_to_confidence(fuzzy_score: float, weight: int) -> float:
    """
    Convert a rapidfuzz 0–100 score + concept weight into a 0–1 confidence.

    Pure numerical mapping — no domain rules. Exact matches land at ~0.9–1.0,
    mid-range fuzzy matches at ~0.4–0.7. Weight gently amplifies strong
    concepts (cardiac arrest weight=10) vs. weak ones (back pain weight=2).
    """
    score_component = max(0.0, min(1.0, fuzzy_score / 100.0))
    # Weight 10 -> +0.1, weight 2 -> +0.02. Caps at 1.0.
    weight_component = max(0.0, min(0.15, weight * 0.01))
    return max(0.0, min(1.0, score_component + weight_component * score_component))
