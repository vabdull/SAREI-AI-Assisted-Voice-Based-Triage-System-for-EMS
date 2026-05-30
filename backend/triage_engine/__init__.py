"""
Layered Arabic EMS triage engine.

Layer 1 (FuzzyMatcher)      — rapidfuzz scan over a normalized YAML keyword
                              bank. Emits matched concepts with character-level
                              evidence spans. ~5–10ms per bubble.

Layer 2 (RuleEngine)        — deterministic aggregation of matches, risk
                              modifier detection, ESI computation, ESI→level
                              mapping. Pure Python, ~1–2ms.

Layer 3 (LLMEnricher)       — asyncio-debounced Qwen call (AITriageAnalysis
                              service) that fires on silence and grounds /
                              corrects the fast-path result.

TriageEngine orchestrates all three against per-case CaseEvidence state and
broadcasts TriageFastEvent + TriageInsightEvent messages via the WS
ConnectionManager.

The engine is hybrid by design: the YAML bank only provides a fast first-
pass. The LLM remains authoritative for final highlights, location, and
clinical reasoning.
"""

from backend.triage_engine.engine import (
    TriageEngine,
    TriageConnectionManager,
    get_triage_engine,
    set_triage_engine,
)
from backend.triage_engine.models import (
    TriageFastEvent,
    TriageInsightEvent,
    TriageMatch,
    TriageEvidenceSpan,
    TriageResetEvent,
    TriageFastResult,
)

__all__ = [
    "TriageEngine",
    "TriageConnectionManager",
    "get_triage_engine",
    "set_triage_engine",
    "TriageFastEvent",
    "TriageInsightEvent",
    "TriageMatch",
    "TriageEvidenceSpan",
    "TriageResetEvent",
    "TriageFastResult",
]
