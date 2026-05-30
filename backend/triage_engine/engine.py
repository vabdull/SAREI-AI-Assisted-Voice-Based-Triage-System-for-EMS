"""
TriageEngine — glues all three layers together and pushes events over WS.

Flow per bubble (silence detector has closed one utterance of ASR):

    1. caller code calls ``engine.process_chunk(case_id, chunk_text)``
    2. engine normalizes + fuzzy-matches the chunk         (Layer 1)
    3. engine merges new matches into the case's CaseEvidence state
    4. engine evaluates rules over the active evidence     (Layer 2)
    5. engine immediately broadcasts a ``triage_update`` WS event
    6. engine schedules a debounced LLM enrichment         (Layer 3)
    7. when Layer 3 fires it broadcasts a ``triage_insight`` WS event

Steps 1–5 are synchronous and bounded by Layer 1 performance (~5–15ms).

Persistence
-----------

On every insight the analysis payload is written back onto the Case
(``case.ai_triage_suggestion`` + ``case.patient_location``) via the existing
``_update_case_analysis`` helper path. That keeps the existing /live-analysis
HTTP endpoint, the dispatcher case summary, and the medic/hospital portals
all working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Optional

from fastapi import WebSocket

from backend.triage_engine.case_evidence import CaseEvidence, CaseEvidenceStore
from backend.triage_engine.keyword_bank import KeywordBank, get_keyword_bank
from backend.triage_engine.llm_enricher import LLMEnricher
from backend.triage_engine.matcher import FuzzyMatcher
from backend.triage_engine.models import (
    TriageFastEvent,
    TriageFastResult,
    TriageInsightEvent,
    TriageMatch,
    TriageRiskModifier,
    TriageResetEvent,
)
from backend.triage_engine.rules import RuleEngine

logger = logging.getLogger(__name__)


class TriageConnectionManager:
    """Case-scoped WebSocket fan-out for triage events.

    This is a dedicated manager (separate from the transcript WS at
    /api/v1/realtime/ws/{case_id}) so the two concerns stay independent.
    """

    def __init__(self) -> None:
        self._conns: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, case_id: int, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns.setdefault(case_id, set()).add(ws)
        logger.info("Triage WS connected for case %s (total=%d)", case_id, self._count(case_id))

    async def disconnect(self, case_id: int, ws: WebSocket) -> None:
        async with self._lock:
            bucket = self._conns.get(case_id)
            if bucket is not None:
                bucket.discard(ws)
                if not bucket:
                    self._conns.pop(case_id, None)
        logger.info("Triage WS disconnected for case %s (total=%d)", case_id, self._count(case_id))

    def _count(self, case_id: int) -> int:
        return len(self._conns.get(case_id, ()))

    async def broadcast(self, case_id: int, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._conns.get(case_id, ()))
        if not targets:
            return
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                bucket = self._conns.get(case_id)
                if bucket is not None:
                    for ws in dead:
                        bucket.discard(ws)
                    if not bucket:
                        self._conns.pop(case_id, None)


class TriageEngine:
    """
    Process-wide triage engine.

    Safe to instantiate multiple times; there is also a cached default
    singleton via :func:`get_triage_engine`.
    """

    def __init__(
        self,
        bank: KeywordBank | None = None,
        *,
        connection_manager: TriageConnectionManager | None = None,
        persistence_callback: Optional[
            Callable[[int, TriageInsightEvent], Awaitable[None]]
        ] = None,
    ) -> None:
        self.bank = bank or get_keyword_bank()
        self.matcher = FuzzyMatcher(self.bank)
        self.rules = RuleEngine(self.bank)
        self.case_store = CaseEvidenceStore(self.bank)
        self.conn_manager = connection_manager or TriageConnectionManager()
        self._persistence_callback = persistence_callback

        self.enricher = LLMEnricher(
            self.bank,
            on_insight=self._on_insight,
        )

        self._chunk_counters: dict[int, int] = {}
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────

    async def process_chunk(self, case_id: int, chunk_text: str) -> TriageFastEvent:
        """Run Layers 1+2 synchronously, schedule Layer 3, broadcast the event."""
        chunk_text = (chunk_text or "").strip()
        if not chunk_text:
            empty = self._empty_event(case_id)
            return empty

        event, fast_result, case = self._evaluate_final_chunk(case_id, chunk_text)

        await self.conn_manager.broadcast(case_id, event.model_dump(mode="json"))

        # Layer 3 — schedule debounced LLM enrichment. Never blocks the caller.
        self.enricher.schedule(
            case_id=case_id,
            full_transcript=case.full_transcript,
            fast_result=fast_result,
        )

        return event

    async def process_preview_chunk(
        self,
        case_id: int,
        chunk_text: str,
        *,
        preview_transcript: str | None = None,
        client_sent_at_ms: float | None = None,
    ) -> TriageFastEvent:
        """
        Run Layers 1+2 for a provisional preview without mutating case evidence.

        This lets the UI surface low-latency highlights/triage while the caller
        is still speaking, but keeps the authoritative case state tied to the
        final silence-closed ASR chunk.
        """
        chunk_text = (chunk_text or "").strip()
        if not chunk_text:
            return self._empty_event(case_id)

        full_transcript = (preview_transcript or chunk_text).strip() or chunk_text
        event = self._build_preview_event(
            case_id=case_id,
            chunk_text=chunk_text,
            full_transcript=full_transcript,
            client_sent_at_ms=client_sent_at_ms,
        )
        await self.conn_manager.broadcast(case_id, event.model_dump(mode="json"))
        return event

    def evaluate_chunk(self, case_id: int, chunk_text: str) -> TriageFastEvent:
        """Pure CPU evaluation of a finalized chunk — no WS broadcast,
        no LLM scheduling.

        Used by ``fast_decision_service`` which owns its own broadcast
        path via ``case_state_service``. The authoritative case
        evidence is still updated here so repeated chunks dedupe and
        the enrichment service can read the cumulative state.
        """
        chunk_text = (chunk_text or "").strip()
        if not chunk_text:
            return self._empty_event(case_id)
        event, _fast_result, _case = self._evaluate_final_chunk(case_id, chunk_text)
        return event

    def evaluate_preview(
        self,
        case_id: int,
        chunk_text: str,
        *,
        preview_transcript: str | None = None,
    ) -> TriageFastEvent:
        """Pure CPU evaluation of a provisional preview chunk.

        Does not mutate the authoritative case evidence store; see
        ``process_preview_chunk`` for the behaviour the transcript WS
        used to call directly.
        """
        chunk_text = (chunk_text or "").strip()
        if not chunk_text:
            return self._empty_event(case_id)
        full_transcript = (preview_transcript or chunk_text).strip() or chunk_text
        return self._build_preview_event(
            case_id=case_id,
            chunk_text=chunk_text,
            full_transcript=full_transcript,
            client_sent_at_ms=None,
        )

    def process_preview_chunk_sync(
        self,
        case_id: int,
        chunk_text: str,
        *,
        preview_transcript: str | None = None,
    ) -> TriageFastEvent:
        """Sync-callable preview evaluation (no WS side effects)."""
        return self.evaluate_preview(
            case_id,
            chunk_text,
            preview_transcript=preview_transcript,
        )

    def process_chunk_sync(self, case_id: int, chunk_text: str) -> TriageFastEvent:
        """
        Synchronous variant safe to call from HTTP handlers.

        We can't ``await`` the broadcast from a sync context, so we schedule
        it onto the running loop if one exists. If no loop is running (e.g.
        unit tests), we still return the computed event — the caller just
        won't see it on the WS. The LLM enrichment is also scheduled onto
        the loop.
        """
        chunk_text = (chunk_text or "").strip()
        if not chunk_text:
            return self._empty_event(case_id)

        event, fast_result, case = self._evaluate_final_chunk(case_id, chunk_text)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            loop.create_task(
                self.conn_manager.broadcast(case_id, event.model_dump(mode="json"))
            )
            # Hand off to the LLM enricher; ``schedule`` is synchronous and
            # debounces/queues the actual async work on the event loop.
            self.enricher.schedule(
                case_id=case_id,
                full_transcript=case.full_transcript,
                fast_result=fast_result,
            )

        return event

    async def reset(self, case_id: int) -> None:
        """Mark a new call start: drop accumulated state + notify clients."""
        self.case_store.reset(case_id)
        await self.enricher.cancel(case_id)
        with self._lock:
            self._chunk_counters[case_id] = 0
        event = TriageResetEvent(case_id=case_id)
        await self.conn_manager.broadcast(case_id, event.model_dump(mode="json"))

    def snapshot(self, case_id: int) -> TriageFastResult:
        """Current cumulative fast result for a case (without running a new chunk)."""
        case = self.case_store.get(case_id)
        return self.rules.evaluate(case.active_matches(), case.all_modifiers())

    # ── Internals ──────────────────────────────────────────────────────

    def _match_and_evaluate(
        self,
        chunk_text: str,
    ) -> tuple[list[TriageMatch], list[TriageRiskModifier], TriageFastResult]:
        t0 = time.perf_counter()
        matches, modifiers, _norm = self.matcher.match(chunk_text)
        processing_ms = (time.perf_counter() - t0) * 1000.0
        fast_result = self.rules.evaluate(
            matches,
            modifiers,
            processing_time_ms=processing_ms,
        )
        return matches, modifiers, fast_result

    def _next_chunk_index(self, case_id: int) -> int:
        with self._lock:
            idx = self._chunk_counters.get(case_id, 0) + 1
            self._chunk_counters[case_id] = idx
        return idx

    def _evaluate_final_chunk(
        self,
        case_id: int,
        chunk_text: str,
    ) -> tuple[TriageFastEvent, TriageFastResult, CaseEvidence]:
        matches, modifiers, _chunk_fast_result = self._match_and_evaluate(chunk_text)

        case = self.case_store.get(case_id)
        case.ingest(matches, modifiers, chunk_text=chunk_text)

        active = case.active_matches()
        all_modifiers = case.all_modifiers()
        fast_result = self.rules.evaluate(active, all_modifiers)
        idx = self._next_chunk_index(case_id)
        event = TriageFastEvent(
            case_id=case_id,
            chunk_index=idx,
            chunk_text=chunk_text,
            full_transcript=case.full_transcript,
            result=fast_result,
            chunk_matches=matches,
            provisional=False,
        )
        return event, fast_result, case

    def _build_preview_event(
        self,
        *,
        case_id: int,
        chunk_text: str,
        full_transcript: str,
        client_sent_at_ms: float | None = None,
    ) -> TriageFastEvent:
        matches, modifiers, fast_result = self._match_and_evaluate(chunk_text)
        return TriageFastEvent(
            case_id=case_id,
            chunk_index=0,
            chunk_text=chunk_text,
            full_transcript=full_transcript,
            result=fast_result,
            chunk_matches=matches,
            provisional=True,
            client_sent_at_ms=client_sent_at_ms,
        )

    def _empty_event(self, case_id: int) -> TriageFastEvent:
        empty_result = self.rules.evaluate([], [])
        return TriageFastEvent(
            case_id=case_id,
            chunk_index=0,
            chunk_text="",
            full_transcript="",
            result=empty_result,
            chunk_matches=[],
            provisional=False,
        )

    async def _on_insight(self, event: TriageInsightEvent) -> None:
        await self.conn_manager.broadcast(event.case_id, event.model_dump(mode="json"))
        callback = self._persistence_callback
        if callback is not None:
            try:
                await callback(event.case_id, event)
            except Exception:
                logger.exception(
                    "Triage persistence callback failed for case %s", event.case_id
                )


# ── Process-wide singleton ─────────────────────────────────────────────

_engine_singleton: TriageEngine | None = None
_engine_lock = threading.Lock()


def get_triage_engine() -> TriageEngine:
    global _engine_singleton
    if _engine_singleton is None:
        with _engine_lock:
            if _engine_singleton is None:
                _engine_singleton = TriageEngine()
                logger.info(
                    "TriageEngine initialized: %d symptoms, %d phrases",
                    len(_engine_singleton.bank.symptoms),
                    len(_engine_singleton.bank.phrase_index),
                )
    return _engine_singleton


def set_triage_engine(engine: TriageEngine) -> None:
    """For tests / DI."""
    global _engine_singleton
    with _engine_lock:
        _engine_singleton = engine
