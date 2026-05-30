"""
Arabic text normalization.

The ASR stack upstream is assumed to already emit normalized text (no
tashkeel, alef variants unified to ا, ya variants unified to ي, ta marbuta
unified to ه). We still re-apply normalization here so that:

  (a) keyword bank entries can be hand-edited without worrying about
      typing a specific alef variant,
  (b) any upstream drift (e.g. a model update) doesn't break the matcher,
  (c) matching happens in a guaranteed single canonical form.

We use pyarabic for tashkeel/tatweel/strip_diacritics because it handles
edge cases (superscript alef, zero-width joiners, etc.) that a hand-rolled
regex would miss. Everything else is done with str.translate for speed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

try:
    from pyarabic import araby
except ImportError:  # pragma: no cover - pyarabic is a required dep
    araby = None  # type: ignore[assignment]


# Unicode replacements applied AFTER tashkeel/tatweel stripping.
#   أ إ آ ٱ ٲ ٳ → ا
#   ى ئ        → ي
#   ة          → ه
#   ؤ          → و
# The set of alef variants we want to collapse. Some ASR models occasionally
# emit uncommon variants; include them defensively.
_ALEF_VARIANTS = "أإآٱٲٳ"
_YAA_VARIANTS = "ىئ"
_TAA_MARBUTA = "ة"
_WAW_HAMZA = "ؤ"

_UNIFICATION_TABLE = str.maketrans(
    {
        **{ch: "ا" for ch in _ALEF_VARIANTS},
        **{ch: "ي" for ch in _YAA_VARIANTS},
        _TAA_MARBUTA: "ه",
        _WAW_HAMZA: "و",
        # Arabic-Indic digits → ASCII digits so the matcher sees "10" == "١٠"
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        # Eastern Arabic-Indic
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    }
)

# Everything considered whitespace-equivalent after normalization.
_WHITESPACE_RE = re.compile(r"\s+")

# Non-printable / bidi / zero-width marks we should drop entirely.
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]")

# Tatweel / kashida
_TATWEEL = "\u0640"


def normalize(text: str) -> str:
    """
    Return the canonical form of ``text`` used by the matcher and keyword
    bank. Safe to call on either Arabic, English, or mixed content — ASCII
    is left alone except for its whitespace being collapsed.
    """
    if not text:
        return ""

    # Drop zero-width / bidi control characters first so diacritic handling
    # doesn't get confused by them.
    text = _ZERO_WIDTH_RE.sub("", text)

    # Drop tatweel / kashida.
    if _TATWEEL in text:
        text = text.replace(_TATWEEL, "")

    # Strip tashkeel using pyarabic, which understands superscript alef and
    # small hamza above/below variants.
    if araby is not None:
        text = araby.strip_tashkeel(text)
        # strip_tatweel is idempotent but cheap and catches anything we missed.
        text = araby.strip_tatweel(text)

    # Unicode NFKC: combine compat forms, normalize digits, etc.
    text = unicodedata.normalize("NFKC", text)

    # Unify alef/ya/ta-marbuta/waw-hamza variants + digits.
    text = text.translate(_UNIFICATION_TABLE)

    # Collapse whitespace last so earlier replacements don't leave stray gaps.
    text = _WHITESPACE_RE.sub(" ", text).strip()

    return text


def normalize_many(texts: Iterable[str]) -> list[str]:
    """Vectorized convenience over :func:`normalize`."""
    return [normalize(t) for t in texts]


def tokens(text: str) -> list[str]:
    """
    Whitespace-delimited token split of ``text`` AFTER normalization.
    Used by the negation window scanner.
    """
    norm = normalize(text)
    if not norm:
        return []
    return norm.split(" ")


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize ``text`` and return an offset map back to the raw input.

    The matcher works on normalized text but the dispatcher UI renders
    the RAW transcript. To paint correct highlight ranges we need a
    way to translate a span found in the normalized string back into
    raw character offsets. This function returns:

        (normalized_text, offset_map)

    where ``offset_map[i]`` is the index in ``text`` corresponding to
    the i-th character of ``normalized_text``. ``offset_map`` has length
    ``len(normalized_text) + 1`` so callers can use it for half-open
    end indices: ``raw_end = offset_map[norm_end]``.

    The behaviour mirrors :func:`normalize` exactly. Character drops
    (diacritics, tatweel, bidi, zero-width) advance the raw pointer
    without emitting a normalized character; substitutions and
    pass-through characters emit one normalized character per one raw
    character; whitespace collapse maps multiple raw whitespace chars
    onto a single normalized space.
    """
    if not text:
        return "", [0]

    if araby is not None:
        tashkeel = set(araby.TASHKEEL)  # type: ignore[attr-defined]
        small_marks = {"\u0670", "\u0653", "\u0654", "\u0655", "\u0656", "\u0657", "\u0658", "\u065F"}
        drop = tashkeel | small_marks
    else:  # pragma: no cover
        drop = set()
    drop.add(_TATWEEL)

    out_chars: list[str] = []
    offset_map: list[int] = []
    last_was_space = True  # collapse leading whitespace
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Drop bidi / zero-width controls entirely.
        if _ZERO_WIDTH_RE.fullmatch(ch):
            i += 1
            continue
        if ch in drop:
            i += 1
            continue
        if ch.isspace():
            if last_was_space:
                i += 1
                continue
            out_chars.append(" ")
            offset_map.append(i)
            last_was_space = True
            i += 1
            continue

        # NFKC may expand into multiple characters; we mirror the same
        # decomposition the bulk normalizer applies. In practice the
        # characters used in Arabic transcripts are atomic under NFKC,
        # so a per-character normalize is the safe and exact approach.
        nfkc = unicodedata.normalize("NFKC", ch)
        # Apply alef/yaa/taa-marbuta/digit unification table.
        mapped = nfkc.translate(_UNIFICATION_TABLE)
        if not mapped:
            i += 1
            continue
        # When NFKC explodes a character (rare for Arabic letters) we
        # still emit each output char and point every one of them back
        # at the same raw index. That keeps highlight ranges stable.
        for out_ch in mapped:
            out_chars.append(out_ch)
            offset_map.append(i)
        last_was_space = False
        i += 1

    # Trim trailing collapsed space.
    while out_chars and out_chars[-1] == " ":
        out_chars.pop()
        offset_map.pop()

    # Append the half-open sentinel offset.
    offset_map.append(n)
    return "".join(out_chars), offset_map


def find_in_raw_with_normalization(
    raw_text: str, raw_needle: str
) -> tuple[int, int] | None:
    """Locate ``raw_needle`` inside ``raw_text`` after normalizing both.

    Returns ``(raw_start, raw_end)`` — offsets into the ORIGINAL
    ``raw_text`` — or ``None`` if the needle does not appear. ``raw_end``
    is exclusive.

    .. deprecated::
        Use :func:`find_phrase_token_aware` instead. This function does
        an unbounded substring search and will happily match ``سم``
        inside ``اسمي`` — that is the root cause of the historical
        false-positive highlights. It is preserved only for legacy
        callers that explicitly want substring semantics (e.g. tests
        of pure normalization behaviour). New code MUST use
        :func:`find_phrase_token_aware`.
    """
    if not raw_text or not raw_needle:
        return None
    norm_text, offset_map = normalize_with_map(raw_text)
    norm_needle = normalize(raw_needle)
    if not norm_needle or not norm_text:
        return None
    idx = norm_text.find(norm_needle)
    if idx < 0:
        return None
    raw_start = offset_map[idx]
    end_norm = idx + len(norm_needle)
    raw_end = offset_map[end_norm] if end_norm < len(offset_map) else len(raw_text)
    return raw_start, raw_end


# ── Arabic word-character + tokenization ──────────────────────────────
#
# A "word character" for our boundary checks covers:
#   • Arabic letters U+0621..U+064A (after normalization the variants
#     أ إ آ ٱ ى ئ ة ؤ are already collapsed, so this range is exhaustive).
#   • U+0671 (ٱ) defensively, in case anything slipped past normalize.
#   • ASCII letters and digits — for mixed-language transcripts.
#
# Punctuation, whitespace, em-dash, Arabic comma (،), period etc. are
# NOT word characters and are valid token boundaries.

_ARABIC_LETTER_RE = re.compile(r"[\u0621-\u064A\u0671A-Za-z0-9]")


def is_arabic_word_char(ch: str) -> bool:
    """Return True if ``ch`` is part of an Arabic / ASCII word token."""
    if not ch:
        return False
    return bool(_ARABIC_LETTER_RE.match(ch))


@dataclass(frozen=True)
class Token:
    """A single normalized whitespace-delimited token.

    Attributes
    ----------
    text:
        The token text in normalized form (no diacritics, unified alef
        / yaa / taa-marbuta, no surrounding punctuation).
    norm_start, norm_end:
        Half-open offsets into the normalized string this token came
        from. Useful for matchers that work in normalized space.
    raw_start, raw_end:
        Half-open offsets into the ORIGINAL raw text. These are what
        the UI uses to paint highlights.
    """

    text: str
    norm_start: int
    norm_end: int
    raw_start: int
    raw_end: int


def tokenize_with_offsets(raw_text: str) -> tuple[str, list[int], list[Token]]:
    """Tokenize ``raw_text`` while preserving normalized→raw offsets.

    Returns ``(norm_text, offset_map, tokens)`` where:

    * ``norm_text`` is the canonical-form string (same as
      :func:`normalize_with_map` part 1),
    * ``offset_map[i]`` maps norm-char ``i`` back to a raw index,
    * ``tokens`` is the list of contiguous runs of Arabic / ASCII
      word characters from ``norm_text``, each carrying both
      normalized and raw offsets.

    The tokenizer treats anything that isn't a word character (see
    :func:`is_arabic_word_char`) as a boundary: whitespace, Arabic
    comma ``،``, period, dash, etc. This is what gives the matcher
    real word boundaries and prevents matches like ``سم``→``اسمي``.
    """
    norm_text, offset_map = normalize_with_map(raw_text)
    tokens: list[Token] = []
    n = len(norm_text)
    i = 0
    while i < n:
        if not is_arabic_word_char(norm_text[i]):
            i += 1
            continue
        start = i
        while i < n and is_arabic_word_char(norm_text[i]):
            i += 1
        end = i
        raw_start = offset_map[start]
        raw_end = offset_map[end] if end < len(offset_map) else len(raw_text)
        tokens.append(
            Token(
                text=norm_text[start:end],
                norm_start=start,
                norm_end=end,
                raw_start=raw_start,
                raw_end=raw_end,
            )
        )
    return norm_text, offset_map, tokens


# ── Clitic-prefix stripping (conservative) ────────────────────────────
#
# Arabic attaches a small set of single-letter prepositions / particles
# directly to the next word: و (and), ف (then), ب (with), ك (like),
# ل (for). The definite article ال also attaches as a prefix, often
# combined with one of the above:
#
#       و+ال  → "وال..."     (and the)
#       ف+ال  → "فال..."     (then the)
#       ب+ال  → "بال..."     (with the)
#       ك+ال  → "كال..."     (like the)
#       ل+ال  → "لل..."      (alef of ال elides after ل → لل)
#
# We strip these conservatively. Two safety rails:
#
#  1. ``min_stem_chars`` (default 4): the resulting stem must be at
#     least this long. This is what stops "وسم" from collapsing into
#     a fake match for "سم" — the stem would be 2 chars.
#
#  2. We only strip when the rest looks like a real Arabic token (not
#     just function noise). Combined with #1 this prevents over-stemming.

_CLITIC_PREFIX_TRIES: tuple[str, ...] = (
    "وال", "فال", "بال", "كال",  # و/ف/ب/ك + ال
    "لل",                           # ل + ال (alef elides)
    "ال",                           # bare definite article
    "و", "ف", "ب", "ك", "ل",      # single-letter particles
)


def strip_clitic_prefix(token: str, *, min_stem_chars: int = 4) -> str:
    """Return ``token`` with at most one Arabic clitic prefix removed.

    Conservative — see module docs. The algorithm:

    1. Find the LONGEST prefix in :data:`_CLITIC_PREFIX_TRIES` that
       ``token`` starts with.
    2. If removing that prefix leaves a stem of at least
       ``min_stem_chars`` characters → return the stem.
    3. Otherwise return ``token`` unchanged.

    We deliberately do NOT fall back to a shorter prefix when the
    longest fails the stem-length guard. Falling back would let
    ``والسم`` collapse to ``السم`` (still a clitic-prefixed form),
    which is neither canonical nor safe to compare against bank
    entries — a recipe for the same class of false positive we're
    eliminating. Refusing the whole strip when the natural prefix
    is too aggressive keeps the function principled.

    Idempotent and safe to call on non-Arabic input.
    """
    if not token:
        return token
    for prefix in _CLITIC_PREFIX_TRIES:
        if not token.startswith(prefix):
            continue
        # Longest matching prefix found (the trie is sorted longest
        # first within each compound family). Decide once.
        stem = token[len(prefix):]
        if len(stem) >= min_stem_chars:
            return stem
        return token
    return token


# ── Token-aware phrase finder ─────────────────────────────────────────


def _normalized_phrase_tokens(raw_needle: str) -> list[str]:
    """Return the list of normalized whitespace-split tokens for a
    phrase, ignoring any leading/trailing punctuation."""
    norm_needle = normalize(raw_needle)
    return [t for t in norm_needle.split(" ") if t]


def _token_matches_phrase_token(
    text_token: str,
    phrase_token: str,
    *,
    allow_clitic_prefix: bool,
    min_stem_chars: int,
) -> bool:
    """Compare a transcript token to a phrase token.

    Equality first; then, if ``allow_clitic_prefix``, retry after
    stripping a single conservative clitic prefix from the transcript
    token. Phrase tokens are NEVER stripped — the bank stores
    canonical forms and we don't want to recover the bank.
    """
    if text_token == phrase_token:
        return True
    if not allow_clitic_prefix:
        return False
    stripped = strip_clitic_prefix(text_token, min_stem_chars=min_stem_chars)
    return stripped == phrase_token


def find_phrase_token_aware(
    raw_text: str,
    raw_needle: str,
    *,
    occurrence: str = "last",
    allow_clitic_prefix: bool = True,
    min_stem_chars: int = 4,
) -> tuple[int, int] | None:
    """Locate ``raw_needle`` inside ``raw_text`` using token boundaries.

    Unlike :func:`find_in_raw_with_normalization`, the match must align
    with whole transcript tokens — so ``سم`` will NOT match inside
    ``اسمي``. Multi-token phrases must match a consecutive run of
    transcript tokens, each token equal (or, with
    ``allow_clitic_prefix=True``, equal after a single-clitic strip)
    to the corresponding phrase token.

    Parameters
    ----------
    raw_text:
        Original transcript text (the UI renders this verbatim).
    raw_needle:
        Bank phrase or LLM span_text (will be normalized internally).
    occurrence:
        ``"first"`` returns the leftmost match; ``"last"`` (default)
        returns the rightmost. The live pipeline prefers the latest
        occurrence so a freshly-arrived chunk re-mentioning a symptom
        highlights the new wording, not the original one.
    allow_clitic_prefix:
        If True, transcript tokens may carry a single clitic prefix
        (و/ف/ب/ك/ل/ال and combinations) and still match the phrase.
        The phrase token itself is never stripped.
    min_stem_chars:
        Minimum length of the stem after clitic stripping; ``4`` blocks
        ``وسم`` → ``سم`` collapsing.

    Returns
    -------
    ``(raw_start, raw_end)`` half-open offsets into ``raw_text``, or
    ``None`` when the needle is not present at any token-aligned
    position.
    """
    if not raw_text or not raw_needle:
        return None
    phrase_tokens = _normalized_phrase_tokens(raw_needle)
    if not phrase_tokens:
        return None
    _norm, _offset_map, text_tokens = tokenize_with_offsets(raw_text)
    if not text_tokens:
        return None
    n_phrase = len(phrase_tokens)
    n_text = len(text_tokens)
    if n_phrase > n_text:
        return None

    hits: list[tuple[int, int]] = []
    for start in range(n_text - n_phrase + 1):
        # First-token match: allow clitic prefix on the leading
        # transcript token only. Subsequent tokens must equal exactly,
        # because Arabic clitics only attach to the FIRST element of a
        # noun phrase (و/ال modify the whole NP, not interior words).
        if not _token_matches_phrase_token(
            text_tokens[start].text,
            phrase_tokens[0],
            allow_clitic_prefix=allow_clitic_prefix,
            min_stem_chars=min_stem_chars,
        ):
            continue
        ok = True
        for k in range(1, n_phrase):
            if text_tokens[start + k].text != phrase_tokens[k]:
                ok = False
                break
        if not ok:
            continue
        raw_start = text_tokens[start].raw_start
        raw_end = text_tokens[start + n_phrase - 1].raw_end
        hits.append((raw_start, raw_end))

    if not hits:
        return None
    if occurrence == "first":
        return hits[0]
    return hits[-1]
