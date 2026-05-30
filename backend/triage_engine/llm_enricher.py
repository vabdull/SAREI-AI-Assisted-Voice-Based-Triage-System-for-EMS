"""
Layer 3 — LLM enricher.

Asyncio-native debounced wrapper around :class:`AITriageAnalysisService`.
The existing service already implements the grounded multi-step LLM pipeline
that returns an :class:`AITriageAnalysis` (highlights, medical entities,
triage level, patient location, reasoning). This enricher just manages the
timing side:

  * ``schedule(case_id, transcript, fast_result)`` is called on every bubble.
  * A timer starts; new bubbles within the debounce window reset it.
  * When the timer fires, the LLM job runs in a background thread
    (``AITriageAnalysisService.analyze_transcript_realtime`` is blocking
    because it uses urllib — we offload via :func:`asyncio.to_thread`).
  * On completion an async callback is invoked with the result.

The debounce is tightened when the current fast-path triage is **red** so
that the most critical calls get enriched faster.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Awaitable, Callable, Optional

from backend.ai.triage_analysis_service import AITriageAnalysisService
from backend.schemas.triage_ai import AITriageAnalysis
from backend.triage_engine.keyword_bank import KeywordBank
from backend.triage_engine.models import (
    TriageFastResult,
    TriageInsightEvent,
)

logger = logging.getLogger(__name__)


class LLMEnricher:
    """Debounced Qwen enrichment keyed by case_id."""

    def __init__(
        self,
        bank: KeywordBank,
        on_insight: Callable[[TriageInsightEvent], Awaitable[None]],
        *,
        service: AITriageAnalysisService | None = None,
    ) -> None:
        self._bank = bank
        self._on_insight = on_insight
        self._service = service or AITriageAnalysisService()

        llm_cfg = bank.pipeline.get("llm_enricher", {})
        self._silence_gap = float(llm_cfg.get("silence_gap_seconds", 2.5))
        self._silence_gap_red = float(llm_cfg.get("silence_gap_seconds_red", 1.2))
        self._timeout = float(llm_cfg.get("llm_timeout_seconds", 30.0))
        self._max_concurrency = max(1, int(llm_cfg.get("max_concurrent_jobs", 4)))

        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        # Per-case pending handles. The asyncio.Task is the debounce sleeper;
        # cancelling it resets the debounce.
        self._pending: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────

    def schedule(
        self,
        case_id: int,
        full_transcript: str,
        fast_result: TriageFastResult,
    ) -> None:
        """Schedule an LLM enrichment for this case, debounced on silence.

        Must be called from within the event loop (or via
        :func:`asyncio.get_running_loop().call_soon_threadsafe`).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - misuse path
            logger.error("LLMEnricher.schedule called outside event loop")
            return

        loop.create_task(
            self._schedule(case_id, full_transcript, fast_result)
        )

    async def _schedule(
        self,
        case_id: int,
        full_transcript: str,
        fast_result: TriageFastResult,
    ) -> None:
        debounce = (
            self._silence_gap_red
            if fast_result.level == "red"
            else self._silence_gap
        )
        async with self._lock:
            existing = self._pending.pop(case_id, None)
            if existing is not None and not existing.done():
                existing.cancel()
            task = asyncio.create_task(
                self._debounced_run(case_id, full_transcript, fast_result, debounce)
            )
            self._pending[case_id] = task

    async def cancel(self, case_id: int) -> None:
        async with self._lock:
            task = self._pending.pop(case_id, None)
        if task is not None and not task.done():
            task.cancel()

    # ── Internals ──────────────────────────────────────────────────────

    async def _debounced_run(
        self,
        case_id: int,
        transcript: str,
        fast_result: TriageFastResult,
        debounce: float,
    ) -> None:
        try:
            await asyncio.sleep(debounce)
        except asyncio.CancelledError:
            return

        async with self._semaphore:
            started = time.perf_counter()
            timed_out = False
            analysis: AITriageAnalysis
            try:
                analysis = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._service.analyze_transcript_realtime, transcript
                    ),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning(
                    "LLM enrichment timed out after %.1fs for case %s",
                    self._timeout,
                    case_id,
                )
                analysis = AITriageAnalysis()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "LLM enrichment failed for case %s", case_id
                )
                analysis = AITriageAnalysis()

            latency_ms = (time.perf_counter() - started) * 1000.0

        event = TriageInsightEvent(
            case_id=case_id,
            full_transcript=transcript,
            analysis=analysis,
            fast_result=fast_result,
            llm_latency_ms=round(latency_ms, 1),
            timed_out=timed_out,
        )
        try:
            await self._on_insight(event)
        except Exception:
            logger.exception(
                "triage_insight callback failed for case %s", case_id
            )

        async with self._lock:
            current = self._pending.get(case_id)
            if current is not None and current.done():
                self._pending.pop(case_id, None)
