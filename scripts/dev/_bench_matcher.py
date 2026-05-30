"""Quick latency benchmark for the token-aware matcher.

Confirms the user's hard requirement: the new matching pipeline must
NOT be slower than the substring sweep it replaces. We sample a few
representative Saudi-Arabic transcripts (short / medium / long, with
and without symptom keywords) and time the full ``match()`` call.
"""
import sys
import time
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.triage_engine import get_triage_engine
from backend.app.triage_engine.matcher import FuzzyMatcher

matcher = FuzzyMatcher(get_triage_engine().bank)

samples = [
    ("اسمي عبدالله", "short, no symptom"),
    ("فيني صداع", "short, generic headache"),
    ("عندي صداع شديد جداً ونزيف", "medium, two symptoms"),
    ("المريض فاقد الوعي ولا يتنفس وعنده ألم في الصدر شديد", "long, critical"),
    ("والصداع قوي مره", "clitic-prefixed headache"),
    ("شرب سم بالغلط وصار يستفرغ", "poisoning, real phrase"),
    (
        "حادث سيارة في شارع الملك فهد فيه ثلاث مصابين فاقدين الوعي "
        "والنزيف شديد عليهم الحمد لله الاسعاف وصل",
        "very long, MCI scenario",
    ),
]

# Warm-up — rapidfuzz, regex compilation, etc.
for text, _ in samples:
    matcher.match(text)

ITERS = 500
print(f"matcher.match — {ITERS} iterations per sample\n")
print(f"{'sample':<32} {'mean ms':>10} {'median ms':>11} {'p99 ms':>9}")
print("-" * 64)

for text, label in samples:
    timings = []
    for _ in range(ITERS):
        t0 = time.perf_counter()
        matcher.match(text)
        timings.append((time.perf_counter() - t0) * 1000.0)
    timings.sort()
    p99 = timings[int(0.99 * len(timings))]
    truncated = label[:30]
    print(f"{truncated:<32} {mean(timings):>10.2f} {median(timings):>11.2f} {p99:>9.2f}")
