"""The rules gigaku's CLI mirrors — pure, no aqt, importable by the repo's pytest.

One rule, two implementations: `lib/anki/{select,store}.py` re-implements each of these on
the CLI side (it cannot import aqt-adjacent code paths at runtime, and the add-on cannot
depend on the repo being present). What keeps them one rule is `tests/test_addon_rules.py`,
which imports BOTH sides and pins their outputs equal — drift now fails the suite instead
of silently scrambling the queue.
"""
import re

# Sort-key component widths. Counter: max group size ~1e3, six digits is ample headroom.
# All-count: morphs per sentence, max seen 27. Clarity: hundredths, 000..100.
COUNTER_PAD, CLARITY_PAD, ALLCOUNT_PAD = 6, 3, 4

# A line that finishes: a full stop, or a sentence-final form. The corpus is subtitle-split
# speech, so a clause that runs on into the next line ends in 、 or a connective and fails.
# (Once a tiebreak in the my-learn pick — the "finished sentence takes the tag inside the
# re-ask noise band" rule, measured in and later removed by the user's decision: the tag
# goes strictly to the highest clarity now. Kept as the corpus utility it also is.)
_SENTENCE_END = re.compile(
    r"(?:[。！？♪]"
    r"|[ぁ-んァ-ヶ一-龥](?:です|ます|ました|ません|でした|だ|ね|よ|な|わ|ぞ|かな|かも|でしょ"
    r"|でしょう|んです|んだ|のだ|ですね|ますね|ですよ|ますよ|よね|ですか|ますか|ください"
    r"|なさい)"
    r")[。！？…]?$"
)


def whole_sentence(text):
    """A finished sentence, not a clause cut out of one by the subtitle split."""
    return bool(_SENTENCE_END.search((text or "").strip()))


def to_int(text):
    try:
        return int((text or "0").strip())
    except ValueError:
        return 0


def to_clarity(text):
    """My Clarity ("0.00".."1.00") → hundredths for the sort key. Anything unusable is 0.

    Unscored cards must land at ``000`` rather than sorting arbitrarily: that is what puts
    them below every judged alternate while leaving am-all-morphs-count to order them among
    themselves, i.e. exactly where they sat before the field existed.
    """
    try:
        value = float((text or "").strip() or 0)
    except ValueError:
        return 0
    return max(0, min(100, round(value * 100)))


def fold(word):
    """The case key every grouping shares. `str.lower`, NOT casefold — ß must survive
    (casefold turns Straße into strasse, a form no source ever writes; lower keeps ß),
    the same choice lib/vocab's `_CACHE_FORM` made at the cache boundary.

    Anki's field search is case-insensitive, so `L` shows Leben and leben as one word —
    while every case-sensitive Python grouping counted two. Measured 2026-08-08: 16
    German words carried TWO my-learn tags each and 67 of 1,516 i+1 notes had a wrong
    My Alternatives, sitting at the wrong place in the queue. Japanese has no case, so
    the seam was invisible for as long as the collection was Japanese. Grouping keys
    fold; the *displayed* morph always keeps its own case.
    """
    return (word or "").lower()


def norm_morph(raw):
    """Bare word behind an am-study-morphs value.

    MvJ writes this field in four shapes: bare ``撮る``, ``Sentence: 撮る``,
    ``Definition: 写す``, and — once a definition has been generated and carries unknowns —
    the composite ``Sentence: 撮る | Definition: 写す``. Words are keyed on the normalized
    form so the same word can't slip through as two variants.

    The composite is the one that used to get through: stripping only a leading prefix left
    ``撮る | Definition: 写す``, so a 💣'd card became its *own* word — split off from its
    alternates for `my-learn`, and unfindable by L. The sentence morph is the word being
    studied, so it wins; a definition-only field falls back to that.
    """
    m = (raw or "").strip()
    if "Sentence:" not in m and "Definition:" not in m:
        return m
    sentence = definition = ""
    for part in m.split("|"):
        part = part.strip()
        if part.startswith("Sentence:"):
            sentence = part[len("Sentence:"):].strip()
        elif part.startswith("Definition:"):
            definition = part[len("Definition:"):].strip()
    return (sentence or definition).strip()


def sort_key(alternates, morph, clarity, all_count):
    """`My Sort Key`. Compared as a *string*, so a component is reached only when
    everything left of it ties — which is the whole design: `n` (how common the word is)
    orders my-learn, the morph separates words, clarity decides inside one word's
    alternates (the L view, where every card shares `n` and morph), and all_count breaks
    clarity ties.

    ``clarity=None`` is the legacy three-part key, written when the collection has no
    My Clarity field at all — the add-on keeps working unscored. Never mix the two shapes
    inside one collection: after the morph one has digits where the other has a space, and
    a string compare runs off into unrelated characters.

    The morph component is `fold`ed: Anki sorts the key as a string, so with case intact
    every `Leben` card ordered ahead of every `leben` card *before* clarity got a say —
    and to the L view (case-insensitive search) those are one word, whose order clarity
    is supposed to decide.
    """
    key = f"{alternates:0{COUNTER_PAD}d} {fold(morph)} "
    if clarity is not None:
        key += f"{clarity:0{CLARITY_PAD}d} "
    return key + f"{all_count:0{ALLCOUNT_PAD}d}"


def learn_pick(cards):
    """The one card of a word that gets the my-learn tag; `cards` are that word's
    ``(clarity, all_count, note_id, sentence)`` tuples. Returns the winning note id.

    **Highest clarity, full stop** — then largest am-all-morphs-count, then lowest note id
    so the choice is stable. Clarity leads because the pick is what the score is *for*:
    the card you study should be the one whose meaning its sentence gives away. Before a
    word is scored every clarity reads 0 and the old length rule applies unchanged.

    History: a "finished sentence takes the tag within the ~6-point re-ask noise band"
    tiebreak lived here for a while (measured in: 47 of 148 words moved, the queue went
    43% → 75% whole sentences) and was removed on the user's decision 2026-08 — the tag
    on anything but the top-clarity card read as wrong in the alternates view, and the
    view is where the tag is judged.
    """
    # Negating the note id makes "lowest id" the last tiebreak without a special case.
    return max(cards, key=lambda c: (c[0], c[1], -c[2]))[2]


# ── which language a morph's script says it is ───────────────────────────────
# Verbatim mirror of lib/vocab/ankimorphs.py's _SCRIPT_RANGES/_lang_of (pinned equal by
# tests/test_addon_rules.py): AnkiMorphs' tables carry no language column, so the lemma's
# own characters are the only witness. Kana, Han, CJK punctuation and the whole fullwidth
# block are Japanese (the collection's non-CJK known lemmas are fullwidth netspeak — ｗ,
# ｋｗｓｋ — measured 2026-08-06, 0 Latin); Latin with the umlaut/ß ranges is German; a
# lemma neither claims gets None, never a guess. ja first: Ｔシャツ is Japanese quoting
# Latin, never the reverse.
SCRIPT_RANGES = {
    "ja": (
        (0x2E80, 0x2EFF), (0x2F00, 0x2FDF), (0x3005, 0x3005), (0x3007, 0x3007),
        (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x3FFFF),
        (0x3000, 0x303F),  # CJK symbols/punctuation
        (0x3040, 0x309F),  # hiragana
        (0x30A0, 0x30FF),  # katakana, including ー
        (0x31F0, 0x31FF),  # small-kana extension
        (0xFF00, 0xFFEF),  # fullwidth forms + halfwidth katakana
    ),
    "de": (
        (0x41, 0x5A), (0x61, 0x7A),
        (0xC0, 0xD6), (0xD8, 0xF6), (0xF8, 0xFF),
        (0x100, 0x17F),
    ),
}
SCRIPT_ORDER = ("ja", "de")


def lang_of(lemma):
    for lang in SCRIPT_ORDER:
        ranges = SCRIPT_RANGES[lang]
        if any(any(lo <= ord(ch) <= hi for lo, hi in ranges) for ch in lemma):
            return lang
    return None


# ── recalc language scope (features/am_recalc.py) ────────────────────────────
# A note-filter row belongs to a language only through its note type; a row that is
# neither language's is nobody's to pause and always survives a scoped recalc.


def recalc_selection(ja_on, de_on):
    """The languages a recalc touches — or None for the states that scope nothing.

    Both boxes ticked is the everything-recalc and needs no interception; both unticked
    is read as a mistake, not as "recalc nothing" — an empty read list would still wipe
    ankimorphs.db on the drop_all_tables that opens cache_anki_data."""
    selected = {lang for lang, on in (("ja", ja_on), ("de", de_on)) if on}
    return selected if len(selected) == 1 else None


def recalc_keeps(note_type, selected, ja_notetype, de_notetype):
    """Whether a recalc scoped to `selected` keeps an AnkiMorphs filter row."""
    if note_type == de_notetype:
        return "de" in selected
    if note_type == ja_notetype:
        return "ja" in selected
    return True


def freq_orders(config):
    """The stored orders, as a list of `{"deck", "cards"}` — old and new shape alike.

    The key held ONE deck (`{"deck": ..., "cards": [...]}`) because one deck had an order.
    A second deck with its own order (the sentence-mining skill's, ranked by how often the
    word occurs in the channel it was mined from) must not cost the first one its replay, so
    the key now also accepts `{"orders": [...]}`. The old shape is still read, because a
    collection written before this change must keep working without being rewritten first.
    """
    if not isinstance(config, dict):
        return []
    if isinstance(config.get("orders"), list):
        return [o for o in config["orders"] if isinstance(o, dict) and o.get("cards")]
    return [config] if config.get("cards") else []


def freq_order_cards(config, still_new):
    """Card ids to reposition after a recalc: the stored order, minus what is no longer new.

    `freqdeck/order.py` writes `{"deck": name, "cards": [id, ...]}` into the collection
    (`gigaku.freqOrder`) because AnkiMorphs' recalc rewrites the due of every new card it
    manages — measured 2026-08-28, the Frequency deck's own order lasted until the next R.
    `features/freq_order.py` replays that list; this decides what of it still applies.

    A stored list is a claim about cards, and cards change: one may have been studied (the
    scheduler owns it now), or deleted. Both simply drop out, order otherwise untouched.
    """
    if not isinstance(config, dict):
        return []
    return [cid for cid in config.get("cards") or [] if still_new(cid)]
