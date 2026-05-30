"""
Layer 2 — Rule engine.

Takes a list of :class:`TriageMatch` plus the list of active risk modifiers and
computes the final ESI level, the red/yellow/green UI label, and any
escalation flag.

The rules are intentionally few and boring:

  * Base ESI = the LOWEST (most critical) ``esi`` among non-negated matches.
  * If any escalating modifier fires, bump ESI by -1 (floor 1).
  * Map ESI → red/yellow/green using ``pipeline.yaml`` ``rules.esi_to_level``.

Everything "smart" happens in Layer 3 (LLM enrichment) — this layer exists
only to give the dispatcher a trustworthy color within ~15ms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from backend.app.triage_engine.keyword_bank import KeywordBank
from backend.app.triage_engine.models import (
    ESI_LABELS_AR,
    TriageFastResult,
    TriageLevel,
    TriageMatch,
    TriageRiskModifier,
)


@dataclass
class RuleConfig:
    esi_to_level: dict[int, str]
    default_esi: int


def _rule_config_from_bank(bank: KeywordBank) -> RuleConfig:
    rules_cfg = bank.pipeline.get("rules", {})
    esi_to_level_raw = rules_cfg.get("esi_to_level") or {}
    esi_to_level: dict[int, str] = {}
    for k, v in esi_to_level_raw.items():
        try:
            esi_to_level[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    # Defensive defaults in case anything is missing.
    for esi in range(1, 6):
        esi_to_level.setdefault(esi, "red" if esi <= 2 else "yellow" if esi == 3 else "green")
    default_esi = int(rules_cfg.get("default_esi", 5))
    return RuleConfig(esi_to_level=esi_to_level, default_esi=default_esi)


def _esi_to_level(esi: int, cfg: RuleConfig) -> TriageLevel:
    value = cfg.esi_to_level.get(esi, "green")
    if value not in ("red", "yellow", "green"):
        value = "green"
    return value  # type: ignore[return-value]


class RuleEngine:
    """Deterministic evaluation over matched symptoms + risk modifiers."""

    def __init__(self, bank: KeywordBank) -> None:
        self.bank = bank
        self.config = _rule_config_from_bank(bank)

    def evaluate(
        self,
        matches: Sequence[TriageMatch],
        modifiers: Sequence[TriageRiskModifier],
        *,
        processing_time_ms: float = 0.0,
    ) -> TriageFastResult:
        # Drop negated matches — they aren't clinical signal, they're
        # anti-signal. We keep them for UI/evidence only.
        active = [m for m in matches if not m.negated]

        if active:
            base_esi = min(m.esi for m in active)
        else:
            base_esi = self.config.default_esi

        escalated = False
        for mod in modifiers:
            if mod.escalate:
                escalated = True
                break

        final_esi = base_esi
        if escalated and base_esi > 1:
            final_esi = base_esi - 1

        level = _esi_to_level(final_esi, self.config)
        label_ar = ESI_LABELS_AR.get(final_esi, "غير معروف")

        # Sort matches by clinical importance: active first, then by ESI asc,
        # then weight desc, then confidence desc.
        ordered_matches = sorted(
            matches,
            key=lambda m: (
                m.negated,  # False < True -> non-negated first
                m.esi,  # lowest ESI = most critical first
                -m.weight,
                -m.confidence,
            ),
        )

        return TriageFastResult(
            esi=final_esi,
            esi_label_ar=label_ar,
            level=level,
            escalated=escalated,
            matches=list(ordered_matches),
            modifiers=list(modifiers),
            processing_time_ms=round(processing_time_ms, 2),
        )
