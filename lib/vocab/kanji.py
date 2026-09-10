"""Kanji you meet in the Japanese words you know — as a TSV table, or as an Anki query.

Ported from polyglotka (simple_commands/kanji.py). The one dependency it had, `regex`, was
there for a single `\\p{Han}` match; the Unicode blocks that property covers are a fixed
list, so `_is_han` checks them directly and gigaku keeps its dependency list at three.
"""
from dataclasses import dataclass, field

from lib.config import settings
from lib.vocab.words import Stage

# The Han (CJK ideograph) blocks, i.e. what \p{Han} matches. Radicals, strokes and the
# compatibility/extension blocks included — a word can legitimately contain any of them.
HAN_RANGES = (
    (0x2E80, 0x2EFF),  # CJK Radicals Supplement
    (0x2F00, 0x2FDF),  # Kangxi Radicals
    (0x3005, 0x3005),  # 々 iteration mark
    (0x3007, 0x3007),  # 〇 ideographic zero
    (0x3400, 0x4DBF),  # Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs — the ones you'll actually hit
    (0xF900, 0xFAFF),  # Compatibility Ideographs
    (0x20000, 0x3FFFF),  # Extensions B–I + compatibility supplement
)


def _is_han(char):
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in HAN_RANGES)


@dataclass
class Kanji:
    char: str
    known: set = field(default_factory=set)  # words containing it that you know
    learning: set = field(default_factory=set)

    @property
    def rank(self):
        # Most known words first, then most learning words, then codepoint for a stable table.
        return (-len(self.known), -len(self.learning), self.char)


def collect(words):
    """Every kanji in your Japanese vocabulary, with the words it appears in, best first."""
    kanji = {}
    for word in words:
        if word.language != "ja":
            continue
        for char in set(word.word):
            if not _is_han(char):
                continue
            entry = kanji.setdefault(char, Kanji(char))
            bucket = entry.known if word.stage == Stage.KNOWN else entry.learning
            bucket.add(word.word)
    return sorted(kanji.values(), key=lambda k: k.rank)


def tsv(kanji):
    rows = [("Kanji", "Known Words", "Learning Words", "Known", "Learning")]
    rows += [
        (k.char, len(k.known), len(k.learning), "、".join(sorted(k.known)), "、".join(sorted(k.learning)))
        for k in kanji
    ]
    return "\n".join("\t".join(map(str, row)) for row in rows)


def anki_query(kanji):
    """An Anki search for the kanji you have the most words for — the ones worth unsuspending.

    ANKI_MIN_COUNTS trims the tail: `--anki-min-counts 7,9` keeps kanji appearing in ≥7 known
    words **and** ≥9 learning words.

    Both thresholds are checked per-field, and every kanji is tested. Comparing the pair as a
    tuple — `(known, learning) < (7, 9)` — reads like the same thing but is lexicographic: it
    only ever consults `learning` when `known` is exactly 7, so a kanji with 50 known and 0
    learning words sails through the very filter meant to exclude it. And the list is sorted
    by known-count first, so an early miss says nothing about what comes after it: no `break`.
    """
    min_known, min_learning = settings.anki_min_counts()
    top = [k for k in kanji if len(k.known) >= min_known and len(k.learning) >= min_learning]

    if not top:
        return f"No kanji met ANKI_MIN_COUNTS={min_known},{min_learning} — try lower numbers."
    ors = " OR ".join(f"{settings.ANKI_KANJI_FIELD}:{k.char}" for k in top)
    return f"{settings.ANKI_FILTERS} ({ors})"
