"""
enrichment_service — revision-bound async LLM enrichment.

Why this exists
---------------
``AITriageAnalysisService`` is a blocking, multi-LLM, multi-second call.
It used to run in three different places:

* ``/live-analysis`` (blocking, on every poll),
* a thread spawned from ``/live-chunk`` (background, no concurrency control),
* the triage engine's ``LLMEnricher`` (asyncio-debounced).

Any one of those finishing last silently overwrote the case row and the
WS state, and none of them knew which transcript revision they were
operating on. That's the root cause of "the triage flipped red -> green
30 seconds into the call".

This service is the ONLY backend owner of LLM enrichment. It:

* runs exclusively off the critical path (``asyncio.to_thread``),
* debounces per-case, collapsing bursts of revisions into one LLM call,
* stamps every result with the ``transcript_revision`` it was based on,
* hands the result to ``case_state_service`` which decides whether to
  apply, ignore as stale, or merge it.

The service itself never mutates the case row, never writes to the DB,
and never broadcasts. It is a pure producer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from backend.ai.triage_analysis_service import AITriageAnalysisService
from backend.schemas.triage_ai import AITriageAnalysis

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentResult:
    """What ``enrichment_service`` emits when an LLM run completes.

    ``failed`` mirrors ``timed_out`` for non-timeout failures (Ollama
    unreachable, JSON parsing crash, etc.). Both flags carry the same
    semantic guarantee to downstream consumers: *do not apply this
    analysis to canonical state*. The fast path keeps owning
    ``display_triage`` until a future revision succeeds.
    """

    case_id: int
    revision: int
    transcript_text: str
    analysis: AITriageAnalysis
    latency_ms: float
    timed_out: bool
    failed: bool = False


EnrichmentCallback = Callable[[EnrichmentResult], Awaitable[None]]


class EnrichmentService:
    """Per-case debounced LLM runner.

    Parameters
    ----------
    on_result
        Awaitable invoked with an :class:`EnrichmentResult` whenever a
        run completes. Typically wired to
        ``case_state_service.apply_enriched``.
    silence_gap_seconds
        Debounce window. We wait this long after the last
        :meth:`schedule` call before firing the LLM, so a caller who is
        actively speaking doesn't make us re-run the LLM every bubble.
    silence_gap_seconds_red
        Tighter debounce used when the fast path already decided this
        is a red call — we want the enrichment sooner.
    timeout_seconds
        Per-call LLM timeout. A timeout drops the run (the fast result
        still owns ``display_triage``), logs a warning, and emits an
        :class:`EnrichmentResult` with ``timed_out=True``.
    max_concurrency
        Hard limit on concurrent LLM calls across all cases.
    """

    def __init__(
        self,
        *,
        on_result: EnrichmentCallback,
        service: AITriageAnalysisService | None = None,
        silence_gap_seconds: float = 2.0,
        silence_gap_seconds_red: float = 1.0,
        timeout_seconds: float = 30.0,
        max_concurrency: int = 2,
    ) -> None:
        self._on_result = on_result
        self._service = service or AITriageAnalysisService()
        self._silence_gap = silence_gap_seconds
        self._silence_gap_red = silence_gap_seconds_red
        self._timeout = timeout_seconds
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._pending: dict[int, asyncio.Task] = {}
        self._last_run_revision: dict[int, int] = {}
        self._lock = asyncio.Lock()
        # Captured at startup so sync HTTP handlers can schedule LLM
        # enrichment without a running event loop on their thread.
        self._main_loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the FastAPI lifespan handler at startup."""
        self._main_loop = loop
        logger.info("enrichment_service bound to event loop id=%s", id(loop))

    # ── Public API ────────────────────────────────────────────────

    def schedule(
        self,
        *,
        case_id: int,
        revision: int,
        transcript_text: str,
        is_red: bool,
    ) -> bool:
        """Request an enrichment for this case/revision.

        Returns ``True`` if the task was scheduled. Callable from BOTH:

        * inside the FastAPI event loop (WS handlers), and
        * sync HTTP worker threads (``/live-chunk``), where we fall back
          to the main loop captured via :meth:`bind_loop`.

        Handling both call sites is required: cases ingested over the
        sync HTTP path must still schedule enrichment on the main loop,
        otherwise their LLM analysis would never reach the dispatcher UI.
        """
        coro_factory = lambda: self._schedule(  # noqa: E731
            case_id, revision, transcript_text, is_red
        )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is not None:
            running.create_task(coro_factory())
            logger.debug(
                "enrichment_service: scheduled case=%s rev=%s (running loop)",
                case_id,
                revision,
            )
            return True

        if self._main_loop is not None and self._main_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro_factory(), self._main_loop)
            logger.debug(
                "enrichment_service: scheduled case=%s rev=%s (main loop, threadsafe)",
                case_id,
                revision,
            )
            return True

        logger.error(
            "enrichment_service: dropping schedule for case=%s rev=%s — "
            "no event loop bound. Did you forget to call "
            "EnrichmentService.bind_loop() at startup?",
            case_id,
            revision,
        )
        return False

    async def cancel(self, case_id: int) -> None:
        async with self._lock:
            task = self._pending.pop(case_id, None)
            self._last_run_revision.pop(case_id, None)
        if task is not None and not task.done():
            task.cancel()

    # ── Internals ─────────────────────────────────────────────────

    async def _schedule(
        self,
        case_id: int,
        revision: int,
        transcript_text: str,
        is_red: bool,
    ) -> None:
        transcript_text = (transcript_text or "").strip()
        if len(transcript_text) < 8:
            return

        # Collapse bursts: if an identical revision is already pending
        # or already ran, do nothing.
        async with self._lock:
            existing = self._pending.get(case_id)
            if existing is not None and not existing.done():
                existing.cancel()
            if self._last_run_revision.get(case_id) == revision:
                # We already produced a result for this exact revision
                # and no newer text has arrived yet.
                return

            debounce = self._silence_gap_red if is_red else self._silence_gap
            task = asyncio.create_task(
                self._debounced_run(case_id, revision, transcript_text, debounce)
            )
            self._pending[case_id] = task

    async def _debounced_run(
        self,
        case_id: int,
        revision: int,
        transcript_text: str,
        debounce: float,
    ) -> None:
        try:
            await asyncio.sleep(debounce)
        except asyncio.CancelledError:
            return

        async with self._semaphore:
            started = time.perf_counter()
            timed_out = False
            failed = False
            analysis: AITriageAnalysis
            try:
                analysis = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._service.analyze_transcript_realtime,
                        transcript_text,
                    ),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning(
                    "enrichment_service: LLM timed out after %.1fs case=%s rev=%s",
                    self._timeout,
                    case_id,
                    revision,
                )
                analysis = AITriageAnalysis()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Treat any non-timeout LLM failure (Ollama down, network
                # error, JSON parse error, etc.) the same as a timeout:
                # emit a result flagged ``failed=True`` so the
                # case_state_service keeps the previous good analysis
                # rather than overwriting it with an empty one. This stops
                # a transient outage from clearing the narrative,
                # highlights, and triage already shown on the UI.
                failed = True
                logger.exception(
                    "enrichment_service: LLM call failed case=%s rev=%s",
                    case_id,
                    revision,
                )
                analysis = AITriageAnalysis()

            latency_ms = (time.perf_counter() - started) * 1000.0

        async with self._lock:
            self._last_run_revision[case_id] = revision
            current = self._pending.get(case_id)
            if current is not None and current.done():
                self._pending.pop(case_id, None)

        result = EnrichmentResult(
            case_id=case_id,
            revision=revision,
            transcript_text=transcript_text,
            analysis=analysis,
            latency_ms=round(latency_ms, 1),
            timed_out=timed_out,
            failed=failed,
        )
        try:
            await self._on_result(result)
        except Exception:
            logger.exception(
                "enrichment_service: on_result callback failed case=%s rev=%s",
                case_id,
                revision,
            )


_service_singleton: Optional[EnrichmentService] = None


def _load_enrichment_settings() -> dict:
    """Pull debounce / timeout / concurrency from ``pipeline.yaml``.

    Returns sane defaults if the config or any specific key is missing
    so unit tests and bootstrap-before-config callers still work.
    """
    defaults = {
        "silence_gap_seconds": 2.0,
        "silence_gap_seconds_red": 1.0,
        "llm_timeout_seconds": 30.0,
        "max_concurrent_jobs": 2,
    }
    try:
        from backend.triage_engine import get_triage_engine

        engine = get_triage_engine()
        bank = engine.bank
        cfg = (bank.pipeline or {}).get("llm_enricher", {}) or {}
        for key, default in defaults.items():
            value = cfg.get(key)
            if value is not None:
                defaults[key] = value
    except Exception:  # pragma: no cover - defensive bootstrap path
        logger.exception(
            "enrichment_service: failed to read llm_enricher config, "
            "falling back to defaults"
        )
    return defaults


def get_enrichment_service(
    *,
    on_result: EnrichmentCallback | None = None,
) -> EnrichmentService:
    """Process-wide singleton. ``on_result`` is bound once (on first call);
    ``case_state_service`` wires it at import time.

    Debounce / timeout / max-concurrency come from
    ``configs/triage/pipeline.yaml`` so tuning is config-driven instead
    of code-driven. The hardcoded constructor defaults remain as a
    last-resort fallback.
    """
    global _service_singleton
    if _service_singleton is None:
        if on_result is None:
            raise RuntimeError(
                "enrichment_service.get_enrichment_service() must be first "
                "called with on_result bound (by case_state_service)."
            )
        cfg = _load_enrichment_settings()
        _service_singleton = EnrichmentService(
            on_result=on_result,
            silence_gap_seconds=cfg["silence_gap_seconds"],
            silence_gap_seconds_red=cfg["silence_gap_seconds_red"],
            timeout_seconds=cfg["llm_timeout_seconds"],
            max_concurrency=cfg["max_concurrent_jobs"],
        )
        logger.info(
            "enrichment_service: configured silence_gap=%.2fs (red=%.2fs) "
            "timeout=%.1fs concurrency=%d (from pipeline.yaml)",
            cfg["silence_gap_seconds"],
            cfg["silence_gap_seconds_red"],
            cfg["llm_timeout_seconds"],
            cfg["max_concurrent_jobs"],
        )
    return _service_singleton
