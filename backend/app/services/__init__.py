"""Backend services.

The live EMS pipeline is owned by four services with strict
responsibilities; the rest of the codebase imports through this
package to avoid pulling from concrete module paths.
"""

from __future__ import annotations

from backend.app.services.case_state_service import (
    CaseStateService,
    get_case_state_service,
)
from backend.app.services.enrichment_service import (
    EnrichmentResult,
    EnrichmentService,
    get_enrichment_service,
)
from backend.app.services.fast_decision_service import (
    FastDecisionResult,
    FastDecisionService,
    get_fast_decision_service,
)
from backend.app.services.transcript_service import (
    TranscriptMergeResult,
    TranscriptService,
    get_transcript_service,
    merge_transcript_text,
)

__all__ = [
    "CaseStateService",
    "get_case_state_service",
    "EnrichmentResult",
    "EnrichmentService",
    "get_enrichment_service",
    "FastDecisionResult",
    "FastDecisionService",
    "get_fast_decision_service",
    "TranscriptMergeResult",
    "TranscriptService",
    "get_transcript_service",
    "merge_transcript_text",
]
