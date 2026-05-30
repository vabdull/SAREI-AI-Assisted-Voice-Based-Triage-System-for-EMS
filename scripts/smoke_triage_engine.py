"""
Quick smoke test for the new triage engine. Runs Layers 1+2 synchronously
against a handful of representative bubbles across the three target dialects
and prints the fast-path result.

Usage:
    .venv\\Scripts\\python.exe -m scripts.smoke_triage_engine
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.triage_engine import TriageEngine
from backend.app.triage_engine.keyword_bank import get_keyword_bank


SCENARIOS = [
    ("Cardiac arrest - Najdi",
     ["ابوي طاح ما يتنفس", "قلبه وقف وما عنده نبض", "المريض عجوز"]),
    ("Chest pain + cardiac history - Hijazi",
     ["عندي وجع في صدري", "انا مريض قلب وعندي دعامه"]),
    ("Stroke - Khaleeji",
     ["امي وجهها مايل", "ما تحرك يدها وكلامها ثقيل"]),
    ("Stabbing - Universal",
     ["في شاب اتطعن", "نزيف شديد من بطنه"]),
    ("Pediatric febrile seizure - Hijazi",
     ["البيبي بيتشنج وحرارته عاليه"]),
    ("Negation should NOT fire cardiac arrest",
     ["الحمدلله قلبه ما وقف"]),
    ("Pregnant with bleeding",
     ["زوجتي حامل وينزل منها دم", "توجعها بطنها كثير"]),
    ("RTA pedestrian hit",
     ["رجال دهسته سياره", "ما يتحرك"]),
    ("Drowning",
     ["طفل صغير غرق في المسبح", "طلعناه وهو ما يتنفس"]),
    ("Mild back pain",
     ["ظهري يوجعني من امس"]),
]


async def main() -> None:
    bank = get_keyword_bank()
    print(f"Loaded bank: {len(bank.symptoms)} symptoms / {len(bank.phrase_index)} phrases\n")

    engine = TriageEngine(bank=bank)

    for label, bubbles in SCENARIOS:
        print("=" * 60)
        print(label)
        await engine.reset(case_id=99)
        for i, bubble in enumerate(bubbles, 1):
            event = engine.process_chunk_sync(case_id=99, chunk_text=bubble)
            r = event.result
            print(f"  bubble #{i}: {bubble!r}")
            print(f"    -> ESI-{r.esi} {r.level:6s} {r.esi_label_ar}"
                  f"  ({r.processing_time_ms:.1f}ms, {len(r.matches)} active)")
            for m in r.matches[:5]:
                tag = "[fuzzy]" if m.is_fuzzy else "[exact]"
                neg = " NEG" if m.negated else ""
                print(f"        {tag}{neg} {m.concept_id:30s} esi={m.esi} "
                      f"score={m.fuzzy_score:5.1f} conf={m.confidence:.2f} "
                      f"kw={m.matched_keyword!r}")
            for mod in r.modifiers:
                print(f"        MOD {mod.modifier_id} ({mod.note_ar}) trig={mod.trigger!r}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
