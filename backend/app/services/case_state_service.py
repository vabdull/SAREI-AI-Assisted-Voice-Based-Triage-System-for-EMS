"""
case_state_service — authoritative per-case live state + explicit merge.

This is the ONLY place in the backend that:

* owns the in-memory ``CaseLiveState`` per case,
* decides which result wins when fast + enriched results disagree,
* emits the canonical ``live_state`` WS payload,
* mirrors the canonical payload onto the Case row for the legacy UI
  fields.

Routes, websockets, the transcript service, the fast decision service,
and the enrichment service NEVER overwrite the case row on their own.
They hand their result to this service and the service decides.

Merge policy
------------

There are four input channels, every one of which stamps its output
with the ``transcript_revision`` it was based on:

* ``apply_preview``      — preview fast path (provisional UI update)
* ``apply_fast``         — final fast path, per silence-closed chunk
* ``apply_enriched``     — LLM result (may be older than fast path)
* ``apply_transport_*``  — pure transcript-only updates

Responsibilities are split cleanly between the two analysis layers:

* The fast deterministic layer drives keyword highlighting only
  (``state.highlights``).
* The LLM ("enriched") layer is the sole owner of the triage badge —
  its level, confidence, and reasoning (see ``_compute_display_triage``).

Until the LLM has produced an analysis the badge stays neutral/pending
(``source="none"``); the fast layer never fills it in. This is a
deliberate trade-off: the badge is more trustworthy but appears only
once the LLM responds rather than instantly.

Stale-revision protection
-------------------------

Every ``apply_*`` method refuses a write if ``revision`` is lower than
the last applied revision for its own channel. That protects against
the "the LLM for rev=3 finished after the LLM for rev=5 and overwrote
the newer analysis" class of regression.

Highlights
----------

Highlights always come from the fast path (revision-grounded) plus any
enriched highlights whose ``span_text`` is still a substring of the
current transcript. Enriched highlights older than ``ENRICHED_STALE_
WINDOW`` are dropped silently.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from fastapi import WebSocket
from sqlalchemy.orm import Session

from backend.app.db.models import Case
from backend.app.db.session import SessionLocal
from backend.app.schemas.live_state import (
    CanonicalLivePayload,
    CaseLiveState,
    DisplayTriage,
    HighlightPayload,
)
from backend.app.schemas.location import PatientLocation
from backend.app.schemas.triage_ai import AITriageAnalysis, HighlightItem
from backend.app.services.enrichment_service import (
    EnrichmentResult,
    EnrichmentService,
    get_enrichment_service,
)
from backend.app.services.fast_decision_service import FastDecisionResult
from backend.app.services.observability import log_merge, log_stage
from backend.app.triage_engine import get_triage_engine
from backend.app.triage_engine.models import ESI_LABELS_AR, TriageFastResult
from backend.app.triage_engine.normalization import (
    find_phrase_token_aware,
)

logger = logging.getLogger(__name__)


# A stale enriched result that is more than this many revisions behind
# the current fast revision is ignored for display purposes. It is still
# recorded on the state so observability can see it arrived.
ENRICHED_STALE_WINDOW = 4


def _location_components_filled(loc: PatientLocation) -> int:
    """Number of non-empty fields in ``loc.components``. Proxy for
    "structured detail"; the fast regex usually populates zero, while a
    good LLM extraction populates several (district, landmark, ...)."""
    comp = loc.components
    if comp is None:
        return 0
    filled = 0
    for value in comp.model_dump().values():
        if isinstance(value, str) and value.strip():
            filled += 1
        elif isinstance(value, list) and any(
            isinstance(v, str) and v.strip() for v in value
        ):
            filled += 1
    return filled


def _merge_patient_count(
    current: int | None, incoming: int | None
) -> int | None:
    """Monotonic-by-default merge for patient_count.

    Why monotonic: during a live call, a later chunk often mentions
    *one of* the patients ("شفت مصاب واحد فاقد الوعي") which the fast
    regex naively read as ``patient_count=1``. Silently overwriting
    the earlier higher count downgraded the dispatch (e.g. EMS sends
    one ambulance instead of three).

    Rules:
    * ``None`` from incoming never clears an existing count.
    * Higher counts always replace lower counts.
    * Lower counts NEVER silently replace higher counts. If the caller
      wants to correct a count downward they must do so explicitly
      (e.g. via dispatcher UI override), not via the fast extractor.
    """
    if incoming is None:
        return current
    if current is None:
        return incoming
    return max(current, incoming)


def _location_is_richer(candidate: PatientLocation, baseline: PatientLocation) -> bool:
    """Is ``candidate`` strictly more detailed than ``baseline``?

    Used in confidence ties to prefer the candidate with richer
    structured detail (``components``) and a longer free-form
    ``raw_text``.
    """
    cand_struct = _location_components_filled(candidate)
    base_struct = _location_components_filled(baseline)
    if cand_struct > base_struct:
        return True
    if cand_struct < base_struct:
        return False
    return len((candidate.raw_text or "").strip()) > len(
        (baseline.raw_text or "").strip()
    )


class CaseStateService:
    """The authoritative live-state owner.

    Instances are process-wide singletons; see
    :func:`get_case_state_service`.
    """

    def __init__(self) -> None:
        self._states: dict[int, CaseLiveState] = {}
        self._connections: dict[int, set[WebSocket]] = {}
        self._lock = threading.RLock()
        self._ws_lock = asyncio.Lock()
        # Enrichment is owned by case_state_service; bind the callback
        # when the service is first retrieved so there is no import
        # cycle with enrichment_service at module load time.
        self._enrichment: EnrichmentService | None = None
        # The main FastAPI event loop, captured once at startup. We need
        # this because the live HTTP handler (`/live-chunk`) is a sync
        # `def` and FastAPI runs it in a worker thread without a running
        # loop. ``asyncio.get_running_loop()`` raises in that thread, so
        # before this was wired the broadcast + enrichment scheduling
        # silently no-op'd on every finalized chunk.
        self._main_loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the FastAPI lifespan handler at startup."""
        self._main_loop = loop
        logger.info("case_state_service bound to event loop id=%s", id(loop))

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return a usable event loop without raising.

        Order of preference:
        1. The currently running loop, if we're inside one.
        2. The main loop captured at startup via ``bind_loop``.
        """
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return self._main_loop

    def _ensure_enrichment(self) -> EnrichmentService:
        if self._enrichment is None:
            self._enrichment = get_enrichment_service(
                on_result=self._on_enrichment_result,
            )
        return self._enrichment

    # ── WS fan-out ────────────────────────────────────────────────

    async def connect(self, case_id: int, websocket: WebSocket) -> None:
        async with self._ws_lock:
            self._connections.setdefault(case_id, set()).add(websocket)

    async def disconnect(self, case_id: int, websocket: WebSocket) -> None:
        async with self._ws_lock:
            bucket = self._connections.get(case_id)
            if bucket is not None:
                bucket.discard(websocket)
                if not bucket:
                    self._connections.pop(case_id, None)

    async def _broadcast(self, case_id: int, payload: dict[str, Any]) -> None:
        async with self._ws_lock:
            targets = list(self._connections.get(case_id, ()))
        if not targets:
            return
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                # A send failure means the client socket is gone; collect
                # it for pruning below. Debug-level so normal disconnects
                # don't spam logs but remain diagnosable.
                logger.debug("Dropping dead WS subscriber case=%s", case_id)
                dead.append(ws)
        if dead:
            async with self._ws_lock:
                bucket = self._connections.get(case_id)
                if bucket is not None:
                    for ws in dead:
                        bucket.discard(ws)
                    if not bucket:
                        self._connections.pop(case_id, None)

    def _schedule_coro(self, coro_factory, *, case_id: int, kind: str) -> bool:
        """Schedule a coroutine from sync OR async context.

        Returns ``True`` if scheduling succeeded, ``False`` otherwise.

        The /live-chunk HTTP route is a sync ``def`` that FastAPI runs in
        a worker thread without a running event loop, so
        ``asyncio.get_running_loop()`` raises. We fall back to the main
        loop captured at startup via ``bind_loop`` and use
        ``run_coroutine_threadsafe`` from worker threads.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            # We are inside an event loop (the async WS handlers).
            running.create_task(coro_factory())
            return True

        if self._main_loop is not None and self._main_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro_factory(), self._main_loop)
            return True

        logger.warning(
            "case_state_service: dropping %s for case=%s — no event loop "
            "available. Did you forget CaseStateService.bind_loop() at "
            "startup?",
            kind,
            case_id,
        )
        return False

    def _schedule_broadcast(self, case_id: int, payload: dict[str, Any]) -> None:
        ok = self._schedule_coro(
            lambda: self._broadcast(case_id, payload),
            case_id=case_id,
            kind="broadcast",
        )
        if ok:
            log_stage(
                stage="broadcast",
                latency_ms=0.0,
                case_id=case_id,
                result_kind=str(payload.get("type", "unknown")),
            )

    # ── State lookup ──────────────────────────────────────────────

    def get(self, case_id: int) -> CaseLiveState:
        with self._lock:
            state = self._states.get(case_id)
            if state is None:
                state = CaseLiveState(case_id=case_id)
                self._states[case_id] = state
            return state

    def reset(self, case_id: int) -> None:
        with self._lock:
            self._states.pop(case_id, None)
        engine = get_triage_engine()
        engine.case_store.reset(case_id)
        self._ensure_enrichment()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and self._enrichment is not None:
            loop.create_task(self._enrichment.cancel(case_id))

    def snapshot_payload(self, case_id: int) -> CanonicalLivePayload:
        state = self.get(case_id)
        return self._build_payload(state, fast_matches=[])

    # ── Apply channels (all revision-aware) ───────────────────────

    def apply_transcript_update(
        self,
        *,
        case_id: int,
        transcript_text: str,
        revision: int,
    ) -> CaseLiveState:
        """Called after ``transcript_service.ingest_chunk`` bumped the
        transcript revision. Does not touch triage.
        """
        with self._lock:
            state = self.get(case_id)
            if revision < state.transcript_revision:
                log_merge(
                    case_id=case_id,
                    revision=revision,
                    decision="ignored_stale",
                    reason="transcript_older",
                    source="transcript",
                )
                return state
            state.transcript_revision = revision
            state.transcript_text = transcript_text
            state.transcript_status = "current" if transcript_text else "empty"
            state.last_source_ts_ms = time.time() * 1000.0
            # Re-ground any existing enriched highlights against the
            # new transcript; drop ones whose span_text no longer
            # appears verbatim. Fast highlights are replaced on the
            # next apply_fast.
            state.highlights = self._reground_highlights(
                transcript_text, state.highlights
            )
            return state

    def apply_fast(
        self,
        *,
        case_id: int,
        fast: FastDecisionResult,
        provisional: bool = False,
    ) -> CanonicalLivePayload:
        """Apply a fast (rule-based) decision.

        If ``provisional`` is True this is a preview result from the
        realtime WS; it updates ``preview_transcript_text`` and
        ``fast_triage`` but does not bump ``fast_triage_revision``.
        """
        with self._lock:
            state = self.get(case_id)

            if not provisional:
                if fast.revision < state.fast_triage_revision:
                    log_merge(
                        case_id=case_id,
                        revision=fast.revision,
                        decision="ignored_stale",
                        reason="fast_older_than_last_applied",
                        source="fast",
                        applied_revision=state.fast_triage_revision,
                    )
                    return self._build_payload(state, list(fast.chunk_matches))

                state.fast_triage = fast.fast_triage
                state.fast_triage_revision = fast.revision
                state.chunk_count += 1
                state.last_fast_ts_ms = time.time() * 1000.0
                state.provisional = False
                state.keywords = fast.keyword_labels
                # Highlighting is a FAST-LAYER-ONLY signal. The
                # deterministic matcher is the single source of
                # highlight truth; LLM ("enriched") highlights are NOT
                # merged in. The LLM still owns triage
                # level/confidence/reasoning via ``display_triage`` —
                # it just no longer paints or reorders highlighted
                # words. This keeps the painted spans precise and stops
                # late LLM highlights from popping in mid-call.
                state.highlights = self._dedupe_highlights(list(fast.highlights))
                if fast.location is not None:
                    # The fast extractor can only upgrade confidence,
                    # never downgrade an enriched high-confidence loc.
                    state.location = self._merge_location(
                        state.location, fast.location, source="fast"
                    )
                    state.location_revision = max(
                        state.location_revision, fast.revision
                    )
                if fast.patient_count is not None:
                    state.patient_count = _merge_patient_count(
                        state.patient_count, fast.patient_count
                    )
                # Patient demographics: first non-empty detection wins so
                # re-extraction churn (or a later ambiguous chunk) can't
                # flip an already-captured value. Dispatcher edits live in
                # the DB and are never overwritten (mirror fills nulls only).
                demo = fast.demographics
                if demo.name and not state.patient_name:
                    state.patient_name = demo.name
                if demo.age is not None and state.patient_age is None:
                    state.patient_age = demo.age
                if demo.gender and not state.patient_gender:
                    state.patient_gender = demo.gender
            else:
                # Preview fast path: don't mutate the canonical
                # transcript or the finalized fast_triage; just
                # cache the preview info so the WS payload carries
                # it. The UI decides how to render preview vs final.
                state.provisional = True

            state.display_triage = self._compute_display_triage(state)
            state.reasoning = list(state.display_triage.reasoning)

        payload = self._build_payload(state, list(fast.chunk_matches))
        self._schedule_broadcast(case_id, payload.model_dump(mode="json"))
        # Legacy triage_update message for the current UI hook. Once
        # the UI is migrated to read `live_state` this can be dropped.
        self._schedule_broadcast(
            case_id,
            self._build_legacy_triage_update(
                state, fast, provisional=provisional
            ),
        )

        if not provisional:
            self._schedule_enrichment(state)
            self._persist_legacy_mirror(state)

        log_merge(
            case_id=case_id,
            revision=fast.revision,
            decision="applied",
            reason="fast_ok",
            source="fast",
            result_kind="preview" if provisional else "fast",
            level=state.display_triage.level,
        )
        return payload

    async def apply_enriched_async(
        self,
        *,
        case_id: int,
        revision: int,
        transcript_text: str,
        analysis: AITriageAnalysis,
        latency_ms: float,
        timed_out: bool,
        failed: bool = False,
    ) -> CanonicalLivePayload:
        """Async wrapper; broadcasts from within the loop."""
        payload = self._apply_enriched(
            case_id=case_id,
            revision=revision,
            transcript_text=transcript_text,
            analysis=analysis,
            latency_ms=latency_ms,
            timed_out=timed_out,
            failed=failed,
        )
        await self._broadcast(case_id, payload.model_dump(mode="json"))
        return payload

    def _apply_enriched(
        self,
        *,
        case_id: int,
        revision: int,
        transcript_text: str,
        analysis: AITriageAnalysis,
        latency_ms: float,
        timed_out: bool,
        failed: bool = False,
    ) -> CanonicalLivePayload:
        with self._lock:
            state = self.get(case_id)
            current_rev = max(state.transcript_revision, state.fast_triage_revision)
            stale = revision < current_rev - ENRICHED_STALE_WINDOW
            older = revision < state.enriched_triage_revision

            if timed_out or failed:
                # Treat any non-success identically: the LLM did not
                # produce trustworthy structured output, so keep the
                # previous enriched analysis (if any) and let the fast
                # path keep owning display_triage.
                log_merge(
                    case_id=case_id,
                    revision=revision,
                    decision="ignored_weaker",
                    reason="enrichment_timed_out" if timed_out else "enrichment_failed",
                    source="enriched",
                )
                return self._build_payload(state, [])

            if older:
                log_merge(
                    case_id=case_id,
                    revision=revision,
                    decision="ignored_stale",
                    reason="enriched_older_than_last_applied",
                    source="enriched",
                    applied_revision=state.enriched_triage_revision,
                )
                return self._build_payload(state, [])

            if stale:
                log_merge(
                    case_id=case_id,
                    revision=revision,
                    decision="ignored_stale",
                    reason="enriched_outside_stale_window",
                    source="enriched",
                    applied_revision=current_rev,
                )
                return self._build_payload(state, [])

            state.enriched_triage = analysis
            state.enriched_triage_revision = revision
            state.last_enriched_ts_ms = time.time() * 1000.0

            # Highlighting is fast-layer only: the enriched LLM
            # analysis still updates triage / location / reasoning
            # below, but it MUST NOT contribute highlights. Keep only
            # the fast matcher's highlights, re-grounded against the
            # current transcript (dangling spans dropped silently).
            state.highlights = self._dedupe_highlights(
                self._reground_highlights(
                    state.transcript_text,
                    [h for h in state.highlights if h.source == "fast"],
                )
            )

            if analysis.patient_location is not None:
                state.location = self._merge_location(
                    state.location, analysis.patient_location, source="enriched"
                )
                state.location_revision = max(state.location_revision, revision)

            state.display_triage = self._compute_display_triage(state)
            state.reasoning = list(state.display_triage.reasoning)

        log_merge(
            case_id=case_id,
            revision=revision,
            decision="applied",
            reason="enriched_ok",
            source="enriched",
            result_kind="enriched",
            level=state.display_triage.level,
            latency_ms=latency_ms,
        )
        self._persist_legacy_mirror(state)
        payload = self._build_payload(state, [])
        # Legacy triage_insight message so the existing UI hook still
        # receives the enriched narrative in its old shape.
        self._schedule_broadcast(
            case_id,
            self._build_legacy_triage_insight(state, latency_ms, timed_out),
        )
        return payload

    def _build_legacy_triage_update(
        self,
        state: CaseLiveState,
        fast: FastDecisionResult,
        *,
        provisional: bool,
    ) -> dict[str, Any]:
        result = fast.fast_triage.model_dump(mode="json")
        return {
            "type": "triage_update",
            "case_id": state.case_id,
            "chunk_index": state.chunk_count,
            "chunk_text": "",
            "full_transcript": state.transcript_text,
            "result": result,
            "chunk_matches": [m.model_dump(mode="json") for m in fast.chunk_matches],
            "provisional": provisional,
        }

    def _build_legacy_triage_insight(
        self,
        state: CaseLiveState,
        latency_ms: float,
        timed_out: bool,
    ) -> dict[str, Any]:
        fast_result = (
            state.fast_triage.model_dump(mode="json")
            if state.fast_triage is not None
            else None
        )
        analysis = self._compose_legacy_analysis(state)
        return {
            "type": "triage_insight",
            "case_id": state.case_id,
            "full_transcript": state.transcript_text,
            "analysis": analysis.model_dump(mode="json"),
            "fast_result": fast_result,
            "llm_latency_ms": round(latency_ms, 1),
            "timed_out": timed_out,
            # ``analyzed_revision`` is the canonical staleness key. The
            # frontend must use this to drop insights older than the
            # revision it has already applied. ``transcript_revision``
            # is the case's authoritative latest revision (handy for
            # observability / debugging).
            "analyzed_revision": state.enriched_triage_revision,
            "transcript_revision": state.transcript_revision,
        }

    # ── Merge helpers ─────────────────────────────────────────────

    def _compute_display_triage(self, state: CaseLiveState) -> DisplayTriage:
        """The triage badge is LLM-OWNED.

        Responsibilities are now cleanly split:

        * The fast deterministic layer is used ONLY for highlighting
          (``state.highlights``) and no longer influences the badge.
        * The LLM ("enriched") analysis is the sole source of the
          triage level, confidence, and reasoning narrative.

        Until the LLM has produced an analysis for the case the badge
        stays in a neutral / pending state (``source="none"``). Note
        the trade-off the operator opted into: the instant fast-path
        red flag no longer pre-fills the badge, so the level appears
        only once the LLM responds.
        """
        enriched = state.enriched_triage
        enriched_level = (
            enriched.triage.level if enriched is not None else None
        )

        # No LLM analysis yet → neutral/pending badge. The fast layer
        # deliberately does NOT fill this in anymore.
        if enriched is None or enriched_level is None:
            return DisplayTriage()

        # ``_apply_enriched`` already rejects stale/older enrichments
        # before they reach ``state.enriched_triage``, so whatever is
        # stored here is the freshest trustworthy LLM analysis — use it
        # verbatim for the badge.
        esi = _level_to_esi(enriched_level)
        return DisplayTriage(
            level=enriched_level,
            esi=esi,
            esi_label_ar=ESI_LABELS_AR.get(esi, "غير طارئ"),
            confidence=enriched.triage.confidence,
            source="enriched",
            reasoning=list(enriched.triage.reasoning),
            needs_confirmation=enriched.triage.needs_confirmation,
        )

    @staticmethod
    def _reground_highlights(
        transcript_text: str, highlights: list[HighlightPayload]
    ) -> list[HighlightPayload]:
        """Re-anchor highlight ranges against the current transcript.

        Uses the **token-aware** Arabic finder, so:

        * Diacritic / alef-variant / yaa-variant differences between
          the original ASR chunk and the merged transcript no longer
          drop legitimate highlights (the original motivation for
          this helper).
        * Substring false positives are rejected — a highlight whose
          span doesn't align with whole transcript tokens is dropped
          rather than re-anchored to garbage. This is what fixes the
          regression where stale ``سم`` highlights re-attached to
          ``اسمي`` after the transcript grew.

        We prefer the LAST token-aligned occurrence so a re-uttered
        symptom highlights the fresh mention.
        """
        if not transcript_text:
            return []
        out: list[HighlightPayload] = []
        for h in highlights:
            anchor = find_phrase_token_aware(
                transcript_text,
                h.span_text,
                occurrence="last",
                allow_clitic_prefix=True,
            )
            if anchor is None:
                continue
            raw_start, raw_end = anchor
            out.append(
                h.model_copy(
                    update={
                        "start": raw_start,
                        "end": raw_end,
                        # Keep the highlight's verbatim text in sync with
                        # what the UI will render so chips and highlights
                        # display identical strings.
                        "span_text": transcript_text[raw_start:raw_end],
                    }
                )
            )
        return out

    @staticmethod
    def _dedupe_highlights(
        highlights: list[HighlightPayload],
    ) -> list[HighlightPayload]:
        seen: set[tuple[int, int, str]] = set()
        out: list[HighlightPayload] = []
        for h in highlights:
            key = (h.start or -1, h.end or -1, h.span_text)
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
        return out

    @staticmethod
    def _merge_location(
        current: PatientLocation | None,
        new: PatientLocation | None,
        *,
        source: str,
    ) -> PatientLocation | None:
        """Pick the best ``PatientLocation`` given the current and incoming
        candidates.

        Policy:

        * If either side is None, return the other.
        * Enriched (LLM) candidates win when their confidence is at
          least as high as the current — the LLM normally produces
          richer structured ``components`` and a more descriptive
          ``raw_text`` than the fast regex.
        * Fast (regex) candidates win only when their confidence is
          STRICTLY higher than the current. On a tie the existing
          location stays, which prevents the weaker / shorter fast
          regex from overwriting a richer enriched location after the
          dispatcher reads a follow-up sentence that re-mentions the
          area.
        * On a near-tie (|Δconfidence| < 0.05) we prefer whichever
          candidate carries the longer ``raw_text`` AND a non-empty
          ``components`` block — that's a proxy for "more detail".
        """
        if new is None:
            return current
        if current is None:
            return new

        delta = new.confidence - current.confidence
        TIE_BAND = 0.05

        if source == "enriched":
            if delta >= 0:
                return new
            if abs(delta) <= TIE_BAND and _location_is_richer(new, current):
                return new
            return current

        # Fast path: don't downgrade a richer enriched location on a
        # mere confidence tie.
        if delta > TIE_BAND:
            return new
        if abs(delta) <= TIE_BAND and _location_is_richer(new, current):
            return new
        return current

    # ── Enrichment wiring ─────────────────────────────────────────

    def _schedule_enrichment(self, state: CaseLiveState) -> None:
        if not state.transcript_text:
            return
        enrichment = self._ensure_enrichment()
        enrichment.schedule(
            case_id=state.case_id,
            revision=state.transcript_revision,
            transcript_text=state.transcript_text,
            is_red=state.display_triage.level == "red",
        )

    async def _on_enrichment_result(self, result: EnrichmentResult) -> None:
        await self.apply_enriched_async(
            case_id=result.case_id,
            revision=result.revision,
            transcript_text=result.transcript_text,
            analysis=result.analysis,
            latency_ms=result.latency_ms,
            timed_out=result.timed_out,
            failed=result.failed,
        )

    # ── Payload + DB mirror ───────────────────────────────────────

    def _build_payload(
        self,
        state: CaseLiveState,
        fast_matches: list,
    ) -> CanonicalLivePayload:
        analysis = self._compose_legacy_analysis(state)
        return CanonicalLivePayload(
            state=state.model_copy(deep=True),
            fast_matches=fast_matches,
            analysis=analysis,
            patient_location=state.location,
            extraction_confidence=state.display_triage.confidence or None,
            live_transcript_text=state.transcript_text or None,
        )

    @staticmethod
    def _compose_legacy_analysis(state: CaseLiveState) -> AITriageAnalysis:
        """Legacy ``AITriageAnalysis`` payload for backwards compat.

        The dispatcher UI still reads ``analysis.highlights``,
        ``analysis.triage``, etc. We synthesise that shape from the
        canonical state so the UI keeps working unchanged.

        ``analysis.highlights`` is sourced ONLY from the canonical
        ``state.highlights`` (fast-layer matcher). Highlighting is a
        fast-only signal, so the LLM's own ``highlights`` are
        intentionally discarded here — every consumer of the legacy
        shape (the /live-analysis poller, the DB mirror) sees exactly
        the same fast-grounded highlights the WS subscribers do, and
        never an LLM-painted span.
        """
        base = state.enriched_triage or AITriageAnalysis()
        legacy = base.model_copy(deep=True)
        legacy.triage.level = state.display_triage.level
        legacy.triage.confidence = state.display_triage.confidence
        legacy.triage.reasoning = list(state.display_triage.reasoning)
        legacy.triage.needs_confirmation = state.display_triage.needs_confirmation
        if state.location is not None:
            legacy.patient_location = state.location

        # Fast-only highlights. The canonical state already strips
        # negated highlights at write time, so no re-filter is needed.
        legacy.highlights = [
            HighlightItem(
                label=h.label,
                canonical_label=h.canonical_label,
                span_text=h.span_text,
                start=h.start,
                end=h.end,
                severity=h.severity,
                negated=h.negated,
                uncertain=h.uncertain,
                current=h.current,
            )
            for h in state.highlights
        ]
        return legacy

    @staticmethod
    def _format_chief_complaint(keywords: list[str]) -> str | None:
        """Join canonical keywords into a single Arabic-friendly string.

        Returns ``None`` when there's nothing to write so we don't
        overwrite an existing chief_complaint with an empty value on
        the very first chunks of a call.
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for k in keywords:
            if not k:
                continue
            stripped = k.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            cleaned.append(stripped)
            if len(cleaned) >= 8:  # cap to keep DB column reasonable
                break
        if not cleaned:
            return None
        return " | ".join(cleaned)

    def _persist_legacy_mirror(self, state: CaseLiveState) -> None:
        """Mirror the canonical state onto the Case row.

        Kept in its own short-lived DB session so the merge
        computation does not hold the case_state lock across DB I/O.
        """
        try:
            with SessionLocal() as db:
                case = db.query(Case).filter(Case.id == state.case_id).first()
                if case is None:
                    return
                legacy_analysis = self._compose_legacy_analysis(state)
                serialized = legacy_analysis.model_dump(mode="json")
                changed = False
                if case.ai_triage_suggestion != serialized:
                    case.ai_triage_suggestion = serialized
                    changed = True
                if state.location is not None:
                    loc_payload = state.location.model_dump(mode="json")
                    if case.patient_location != loc_payload:
                        case.patient_location = loc_payload
                        changed = True
                # Mirror the canonical keywords onto ``chief_complaint``
                # so the medic and hospital portals don't show a blank
                # field. The dispatcher UI shows the exact same list
                # (``summarySymptoms``), so we use canonical state as
                # the single source of truth. If the dispatcher later
                # writes free-text notes via the dispatch endpoint that
                # has its own path.
                new_chief = self._format_chief_complaint(state.keywords)
                if new_chief and case.chief_complaint != new_chief:
                    case.chief_complaint = new_chief
                    changed = True
                # Mirror the canonical patient count. Live merge is
                # already monotonic (see ``_merge_patient_count``) so
                # we never silently downgrade a higher earlier count.
                if state.patient_count is not None and case.patient_count != state.patient_count:
                    case.patient_count = state.patient_count
                    changed = True
                # Mirror extracted demographics into EMPTY case fields
                # only. We never overwrite a value already on the case so
                # a dispatcher's manual edit (Edit Call Info) always wins.
                if state.patient_name and not case.patient_name:
                    case.patient_name = state.patient_name
                    changed = True
                if state.patient_age is not None and case.patient_age is None:
                    case.patient_age = state.patient_age
                    changed = True
                if state.patient_gender and not case.patient_gender:
                    case.patient_gender = state.patient_gender
                    changed = True
                conf = state.display_triage.confidence or None
                if case.ai_confidence != conf:
                    case.ai_confidence = conf
                    changed = True
                if changed:
                    db.commit()
        except Exception:
            logger.exception(
                "case_state_service: legacy mirror write failed case=%s",
                state.case_id,
            )


def _level_to_esi(level: str) -> int:
    return {"red": 2, "yellow": 3, "green": 5}.get(level, 5)


_service_singleton: CaseStateService | None = None
_service_lock = threading.Lock()


def get_case_state_service() -> CaseStateService:
    global _service_singleton
    if _service_singleton is None:
        with _service_lock:
            if _service_singleton is None:
                _service_singleton = CaseStateService()
                # Force wiring of the enrichment callback so the first
                # schedule() call does not trip the "must be bound"
                # runtime error.
                _service_singleton._ensure_enrichment()
    return _service_singleton
