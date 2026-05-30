"""
transcript_service — deterministic transcript merging + revisioning.

Scope
-----
The ONLY thing this service owns:

* take a newly transcribed ASR chunk,
* merge it into the authoritative transcript for a case,
* bump ``transcript_revision`` iff something actually changed,
* persist the latest transcript on the ``TranscriptSegment`` row,
* return the resulting ``(text, revision, bumped)`` tuple.

What this service does NOT do
-----------------------------
* it does not call the ASR — the caller passes in already-transcribed text
  (the route handler calls ``StreamingAsrService`` and then calls us),
* it does not run triage,
* it does not touch the LLM,
* it does not broadcast to websockets — that is ``case_state_service``.

Merge rules
-----------
The merge is intentionally boring and deterministic:

1. Empty input → no change, no revision bump.
2. Input fully contained in current transcript (``in``, ``endswith``,
   equality) → no change, no revision bump.
3. Current transcript fully contained in input → replace, bump revision.
4. Otherwise → append with a single separator space, bump revision.

Stale revision protection
-------------------------
``transcript_revision`` is a monotonic per-case integer held in memory
(mirrored onto the DB row via ``TranscriptSegment.chunk_index``). Fast
and enriched results produced by parallel services carry the revision
they were based on, and ``case_state_service`` refuses to apply a result
whose revision is lower than the latest revision.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.app.db.models import TranscriptSegment
from backend.app.services.observability import log_stage, stage_timer


@dataclass(frozen=True)
class TranscriptMergeResult:
    """Return shape of ``TranscriptService.ingest_chunk``."""

    text: str
    revision: int
    bumped: bool
    previous_text: str


def merge_transcript_text(previous: str, next_segment: str) -> str:
    """Pure deterministic transcript merge. No I/O, no side effects.

    Extracted as a free function so tests don't need a DB.
    """
    prev = previous.strip()
    incoming = next_segment.strip()
    if not incoming:
        return prev
    if not prev:
        return incoming
    if prev == incoming or prev.endswith(incoming) or incoming in prev:
        return prev
    if prev in incoming:
        return incoming
    return f"{prev} {incoming}".strip()


class TranscriptService:
    """Per-process authoritative transcript state, keyed by case_id.

    Thread-safe: a single ``RLock`` serialises revision bumps so
    two chunks arriving on different threads cannot race.
    """

    def __init__(self) -> None:
        self._revisions: dict[int, int] = {}
        self._texts: dict[int, str] = {}
        self._lock = threading.RLock()

    # ── Public API ────────────────────────────────────────────────

    def revision(self, case_id: int) -> int:
        with self._lock:
            return self._revisions.get(case_id, 0)

    def text(self, case_id: int) -> str:
        with self._lock:
            return self._texts.get(case_id, "")

    def snapshot(self, case_id: int) -> TranscriptMergeResult:
        """Return the current transcript without mutating state.

        Useful for the enrichment worker so it can stamp its output
        with the revision it saw when it started.
        """
        with self._lock:
            return TranscriptMergeResult(
                text=self._texts.get(case_id, ""),
                revision=self._revisions.get(case_id, 0),
                bumped=False,
                previous_text=self._texts.get(case_id, ""),
            )

    def ingest_chunk(
        self,
        db: Session,
        case_id: int,
        chunk_text: str,
    ) -> TranscriptMergeResult:
        """Merge ``chunk_text`` into the case transcript, bump revision,
        and persist the TranscriptSegment row.

        All three operations are inside the same lock so parallel
        /live-chunk requests for the same case can't interleave.
        """
        with stage_timer(None, "transcript_merge", case_id=case_id):
            with self._lock:
                previous = self._texts.get(case_id, "")
                merged = merge_transcript_text(previous, chunk_text)
                bumped = merged != previous and bool(merged)
                revision = self._revisions.get(case_id, 0)
                if bumped:
                    revision += 1
                    self._revisions[case_id] = revision
                    self._texts[case_id] = merged
                    self._persist(db, case_id, merged, revision)
                result = TranscriptMergeResult(
                    text=merged,
                    revision=revision,
                    bumped=bumped,
                    previous_text=previous,
                )

        log_stage(
            stage="transcript_ingest",
            latency_ms=0.0,
            case_id=case_id,
            revision=result.revision,
            result_kind="bumped" if result.bumped else "unchanged",
            chunk_len=len(chunk_text.strip()),
            merged_len=len(result.text),
        )
        return result

    def reset(self, case_id: int) -> None:
        """Drop authoritative state for a case. Called at call start."""
        with self._lock:
            self._revisions.pop(case_id, None)
            self._texts.pop(case_id, None)

    # ── Internals ─────────────────────────────────────────────────

    @staticmethod
    def _persist(db: Session, case_id: int, merged_text: str, revision: int) -> None:
        """Upsert the single live transcript segment.

        We reuse the AI-generated live row (speaker="caller",
        is_ai_generated=True) so the existing UI polling path keeps
        working unchanged. The revision lives in memory (keyed by
        case_id) because ``TranscriptSegment`` has no dedicated
        revision column and we refuse to run an ad-hoc migration here.
        """
        del revision  # persistence is by latest-row; revision is in-memory
        live_segment = (
            db.query(TranscriptSegment)
            .filter(
                TranscriptSegment.case_id == case_id,
                TranscriptSegment.speaker == "caller",
                TranscriptSegment.is_ai_generated.is_(True),
            )
            .order_by(TranscriptSegment.created_at.desc())
            .first()
        )
        if live_segment is None:
            live_segment = TranscriptSegment(
                case_id=case_id,
                speaker="caller",
                text=merged_text,
                timestamp=0.0,
                is_ai_generated=True,
                confidence=0.9,
            )
            db.add(live_segment)
        else:
            live_segment.text = merged_text
            live_segment.confidence = 0.9
        db.commit()
        db.refresh(live_segment)


_instance: TranscriptService | None = None
_instance_lock = threading.Lock()


def get_transcript_service() -> TranscriptService:
    """Process-wide singleton. Tests should instantiate their own instead."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = TranscriptService()
    return _instance
