"""
Arabic negation-window scanner.

For every matched symptom span we look at the whitespace-tokens immediately
BEFORE the span. If any of them is a negation particle (ما / مش / مو / لا / …),
the match is flagged ``negated=True``. Negated matches are still surfaced to
the dispatcher as evidence, but they do not count toward the ESI calculation.

This is intentionally a tiny, narrow utility — not a full Arabic morphology
stack. It exists purely to prevent embarrassing false positives like "قلبه
ما وقف" getting labeled as cardiac arrest.
"""

from __future__ import annotations

from typing import Iterable


def is_negated(
    normalized_text: str,
    span_start: int,
    particles: Iterable[str],
    window_tokens: int = 3,
) -> bool:
    """
    Return True if any negation particle appears within ``window_tokens``
    whitespace-separated tokens *before* ``span_start`` in ``normalized_text``.

    Both ``normalized_text`` and ``particles`` must already be normalized
    (use :func:`normalization.normalize`).
    """
    if span_start <= 0 or window_tokens <= 0:
        return False

    particle_set = {p for p in particles if p}
    if not particle_set:
        return False

    before = normalized_text[:span_start]
    # Split on whitespace. Keep only the last ``window_tokens`` tokens since
    # anything earlier is by definition too far away.
    tokens = before.rsplit(maxsplit=window_tokens)
    # rsplit returns [leading_junk, tok_-N, ..., tok_-1] when there are
    # enough tokens; if the prefix is shorter, we just get all of them.
    if len(tokens) > window_tokens:
        tokens = tokens[-window_tokens:]

    # Check each token + common two-token negation compounds ("ما عاد").
    joined_tail = " ".join(tokens)
    for particle in particle_set:
        if not particle:
            continue
        # Exact token match
        if particle in tokens:
            return True
        # Particle is multi-word (e.g. "ما عاد"): check suffix match
        if " " in particle and joined_tail.endswith(particle):
            return True
    return False
