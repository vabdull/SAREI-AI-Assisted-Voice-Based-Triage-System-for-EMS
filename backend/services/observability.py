"""
Structured logging + timing for the live EMS pipeline.

Why this exists
---------------
The live path used to emit free-form log lines like "Live chunk received"
scattered across 4 modules. That made it impossible to answer questions
like "what was the end-to-end latency of chunk #42 for case #17?" or
"did the enriched result tied to revision=5 arrive before revision=6?".

This module centralises three things:

1. A single structured field schema (case_id, revision, stage, chunk_id,
   latency_ms, result_kind, merge_decision, ...).
2. A ``stage_timer`` context manager that records per-stage latencies into
   a ``LatencyMetrics`` dataclass.
3. Lightweight helpers (``log_stage``, ``log_merge``) that make every line
   diffable, grep-able, and machine parseable.

All services (transcript_service, fast_decision_service, enrichment_service,
case_state_service) use this module so merge decisions become auditable.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger("ems.live")


@dataclass
class LatencyMetrics:
    """Per-chunk stage latencies in milliseconds.

    Populated incrementally by ``stage_timer``; exposed on
    ``CaseLiveState`` so downstream tooling can monitor drift.
    """

    received_ms: float = 0.0
    preprocess_ms: float = 0.0
    asr_ms: float = 0.0
    transcript_merge_ms: float = 0.0
    fast_decision_ms: float = 0.0
    persist_ms: float = 0.0
    enrich_schedule_ms: float = 0.0
    enrich_run_ms: float = 0.0
    total_critical_ms: float = 0.0

    extra: dict[str, float] = field(default_factory=dict)

    def record(self, stage: str, elapsed_ms: float) -> None:
        attr = {
            "received": "received_ms",
            "preprocess": "preprocess_ms",
            "asr": "asr_ms",
            "transcript_merge": "transcript_merge_ms",
            "fast_decision": "fast_decision_ms",
            "persist": "persist_ms",
            "enrich_schedule": "enrich_schedule_ms",
            "enrich_run": "enrich_run_ms",
            "total_critical": "total_critical_ms",
        }.get(stage)
        if attr is not None:
            setattr(self, attr, round(elapsed_ms, 2))
        else:
            self.extra[stage] = round(elapsed_ms, 2)


@contextmanager
def stage_timer(
    metrics: LatencyMetrics | None,
    stage: str,
    *,
    case_id: int | None = None,
    revision: int | None = None,
    chunk_id: int | None = None,
    log: bool = True,
) -> Iterator[None]:
    """Time a pipeline stage and optionally emit a structured log line."""
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if metrics is not None:
            metrics.record(stage, elapsed_ms)
        if log:
            log_stage(
                stage=stage,
                latency_ms=elapsed_ms,
                case_id=case_id,
                revision=revision,
                chunk_id=chunk_id,
            )


def log_stage(
    *,
    stage: str,
    latency_ms: float,
    case_id: int | None = None,
    revision: int | None = None,
    chunk_id: int | None = None,
    result_kind: str | None = None,
    **extra: Any,
) -> None:
    """Structured stage log line.

    Output format is a single fixed line:
        live_stage case=17 rev=12 chunk=42 stage=asr latency_ms=83.4 ...

    Grep-friendly (stage=asr), parseable by awk, and each field survives
    copy-paste into a dashboard.
    """
    parts: list[str] = ["live_stage"]
    if case_id is not None:
        parts.append(f"case={case_id}")
    if revision is not None:
        parts.append(f"rev={revision}")
    if chunk_id is not None:
        parts.append(f"chunk={chunk_id}")
    parts.append(f"stage={stage}")
    parts.append(f"latency_ms={latency_ms:.2f}")
    if result_kind is not None:
        parts.append(f"result={result_kind}")
    for key, value in extra.items():
        parts.append(f"{key}={value}")
    logger.info(" ".join(parts))


def log_merge(
    *,
    case_id: int,
    revision: int,
    decision: str,
    reason: str,
    source: str,
    result_kind: str | None = None,
    applied_revision: int | None = None,
    **extra: Any,
) -> None:
    """Structured merge-decision log.

    decision ∈ {"applied", "ignored_stale", "ignored_weaker", "merged",
                "preserved_worst", "rejected_ungrounded"}.
    """
    parts: list[str] = [
        "live_merge",
        f"case={case_id}",
        f"rev={revision}",
        f"decision={decision}",
        f"reason={reason}",
        f"source={source}",
    ]
    if result_kind is not None:
        parts.append(f"result={result_kind}")
    if applied_revision is not None:
        parts.append(f"applied_rev={applied_revision}")
    for key, value in extra.items():
        parts.append(f"{key}={value}")
    logger.info(" ".join(parts))
