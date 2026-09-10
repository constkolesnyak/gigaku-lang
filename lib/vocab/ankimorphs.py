"""AnkiMorphs as a word source — the Japanese you learnt in Anki, with the date you did it.

Why this exists: the word cache only ever knew Migaku and Language Reactor, and Japanese
stopped living there. Measured 2026-08-01 — the cache's newest Japanese word was
2026-06-14, Migaku's own `ja` rows hadn't moved since the same day, and the weekly report
had been printing `+0 this week · last word 6 weeks ago` about a language being studied
daily. It was studied in **Anki**, which never fed anything back: `words.save_known_morphs`
*writes* gigaku's known words out to AnkiMorphs, and there was no return path. This is it.

**The contract is that the two counts agree**: whatever AnkiMorphs calls known, gigaku
knows — `gigaku words --lang ja --stage known | wc -l` and the add-on's `L:` are the same
number, 3,797 on the day this was written. That is the user's requirement (2026-08-01) and
it is what makes this module three filters shorter than it was. Nothing is excluded at all;
the only thing this source does *not* do is re-emit a word the cache already holds, which
is dedup, not filtering. Since German joined the collection (2026-08-06) the contract is
per script: the add-on's `L:` is the whole table, and each configured language's count is
its script's share of it (`_lang_of`) — the shares sum back to `L:` minus the lemmas no
configured language claims, which are counted on their own note line.

Two rules were tried and removed, both by measurement, both on the same day. **Evidence** —
a card the user had marked known or answered by hand — reasoning that most of AnkiMorphs'
known set is gigaku's own reflection (3,099 of 3,797 lemmas come from the two files in
`known-morphs/`, one of which this project wrote). The reasoning was right and the rule was
still wrong: it threw away **392 real words** (あっという間, おそらく, もう一度, それぞれ)
that were in neither CSV and simply had no hand-marked card. Then **subtracting
`removed-garbage.csv`**, on the grounds that a "never show me this morph again" list is the
opposite of knowledge: true, and it was exactly the 752 morphs standing between 3,045 and
3,797. The count agreeing beats the list being clean. **Don't re-add either filter.**

One consequence to know: `words.save_known_morphs` writes gigaku's known words back out to
`known-morphs/gigaku_known_morphs_ja.csv`, so once a garbage morph is imported it is
re-exported as known — which is why the curation in `removed-garbage.csv` stopped having
any effect, and why the user deleted that file outright (2026-08-01; it survives in
vocab-backup's history). The two sides converge on purpose — that is what "always the
same number" means.

What the collection forced, and what the code therefore does:

**Anki does not record when a morph became known, so most words are stamped
`UNKNOWN_DATE`.** `Morphs` is lemma, inflection and two interval columns — not one
timestamp anywhere — and a review date does not exist either: of 462,487 cards, **12** have
`prop:ivl>=1` and **16** have ever been answered. This collection is marked known by hand,
not reviewed. Exactly one thing carries a believable date: the **305 cards the user actually
touched** (`tag_known_manually`, or answered), and a morph on one of those is dated by the
earliest of them — 769 morphs. Everything else gets year 1.

Dating the other 1,844 by the earliest `mod` of *any* card they appear on was tried, and
looked convincing: every morph got a date and they spread over seven working days. It is
junk. A card's `mod` is when AnkiMorphs' recalc last rewrote that card, not when the word
was learnt, so "525 words on 22 July" only means "the user was in Anki on 22 July" — and for
a common morph sitting on ten thousand cards the number is meaningless. Rejected by the user
(2026-08-01) in favour of an obviously absent date over a plausible wrong one. **Don't date
a morph from a card the user never touched.**

`UNKNOWN_DATE` is the year 1 (`lib/vocab/words.py`): it sorts before every real word, can
never fall inside a report's week, and is unmistakable on sight in words.json. Anything
drawing a timeline folds it into the left edge — `plots.build` does, or the axis would run
back two thousand years.

**The lemma→card join ignores inflection.** A lemma can be known through one inflection and
carded under another, and the lemma is what becomes a word. Measured in this collection the
looser join changes nothing — 0 extra morphs — so it is correctness, not coverage; the 1,184
morphs with no card have none under either join.

**A word the cache already holds is not re-emitted.** That is the dedup, and it is also
what keeps dates safe: `merge()` takes the latest timestamp per word, so re-sending a word
Migaku dated in November with a July card-mod would walk it forward and count it as new
this week. This source adds words; it never re-dates one.

Reading `ankimorphs.db` directly is safe in a way `collection.anki2` is not: it is the
add-on's, not Anki's, and it is copied through SQLite's own backup API before being queried
(the same move `lib/vocab/backup.py` makes) so a running Anki can't be disturbed.

**Card timestamps are the one thing here that is Anki's own, and they no longer require
Anki to be running.** They come over AnkiConnect when Anki is up, and are read out of
`collection.anki2` when it is not (`_evidence_dates`) — the two answers were measured
identical on the live collection. That matters because this is the only reason the whole
nightly `gigaku backup` ever needed a GUI: from 2026-08-06 the scheduled run was wrapped in
an `open -jga Anki`, so Anki launched itself at 00:05 every night for one `findCards` query
that most nights has nothing to date at all. Everything else a backup reads is a file.
"""
import datetime as dt
import json
import os
import sqlite3
import tempfile

from lib.config import UserError, note, settings
from lib.vocab.kanji import HAN_RANGES
from lib.vocab.words import UNKNOWN_DATE, Stage, Word

PROFILE_SETTINGS = "ankimorphs_profile_settings.json"
DB = "ankimorphs.db"
# AnkiMorphs' default; the profile's own settings file overrides it (never the add-on's
# config.json — the live values are per-profile, see CLAUDE.md).
DEFAULT_INTERVAL = 1
DEFAULT_MANUAL_TAG = "_card-status::i+0-manually"

# The script is the language's witness. AnkiMorphs' tables have no language column —
# `Morphs` is lemma, inflection and two intervals — so once the collection holds two note
# filters (🇯🇵 MvJ under MeCab and 🇩🇪 German under spaCy) a lemma's own characters are the
# only thing that can say which language it belongs to. Kana, Han, CJK punctuation and the
# whole fullwidth block are Japanese: the collection's non-CJK "morphs" are fullwidth
# netspeak (ｗ, ｋｗｓｋ, ｒｐｇ — measured 2026-08-06, 7 of 3,800 known lemmas, 0 Latin),
# which is Japanese vocabulary, not German. Plain Latin, with the Latin-1/Extended-A
# letters German needs (ä ö ü ß, plus loanword accents), is German. A lemma neither
# script claims — or whose script's language isn't configured — is skipped and counted,
# never stamped with a language it doesn't have.
_SCRIPT_RANGES = {
    "ja": HAN_RANGES + (
        (0x3000, 0x303F),  # CJK symbols/punctuation — 、 is a known "morph" in this db
        (0x3040, 0x309F),  # hiragana
        (0x30A0, 0x30FF),  # katakana, including ー
        (0x31F0, 0x31FF),  # small-kana extension
        (0xFF00, 0xFFEF),  # fullwidth forms + halfwidth katakana
    ),
    "de": (
        (0x41, 0x5A), (0x61, 0x7A),                 # A–Z a–z
        (0xC0, 0xD6), (0xD8, 0xF6), (0xF8, 0xFF),   # Latin-1 letters: ä ö ü ß é …
        (0x100, 0x17F),                             # Latin Extended-A, stray loanwords
    ),
}
# ja is checked first: a lemma mixing scripts (Tシャツ) is Japanese quoting Latin — the
# reverse never happens, German has no reason to reach for kana.
_SCRIPT_ORDER = ("ja", "de")


def _lang_of(lemma):
    for lang in _SCRIPT_ORDER:
        ranges = _SCRIPT_RANGES[lang]
        if any(any(lo <= ord(ch) <= hi for lo, hi in ranges) for ch in lemma):
            return lang
    return None


# What a lemma looks like in the cache is per language. German arrives under two
# conventions — Migaku lowercases its dictForms (ß preserved: abschießen, measured
# 2026-08-06) while spaCy capitalizes nouns (abend → Abend drifted 5,308 of 7,290 cache
# words, nearly all by case alone) — and the cache's incumbent is Migaku, with ten months
# of dated words in it. So German lemmas are lowered at this boundary: dedup, emission and
# the merge shield all see one form, and the cache can never grow an Abend beside its
# abend. The known-morphs CSV is the *opposite* boundary and keeps spaCy's capitals — it
# is matched against what recalc lemmatizes, so a lowered row would be inert for every
# noun (`words.save_known_morphs` owns that file — de rows go through `_de_lemmas`; an
# earlier note here credited a build_known_de.py that never existed). The read side must
# then recognise the CSV's own echo — `words._de_exported_forms` — or every lemma whose
# lowered form isn't a cache word returns as a new word at the year 1 (594 in three
# days, measured and cleaned 2026-08-08).
_CACHE_FORM = {"de": str.lower}


def _profile_settings(profile):
    path = os.path.join(profile, PROFILE_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        note(f"AnkiMorphs: no readable {PROFILE_SETTINGS} ({exc}) — using defaults.")
        return {}


def _known_lemmas(profile, threshold):
    """Every lemma AnkiMorphs calls known → the cards it appears on.

    Copied through SQLite's backup API rather than opened in place: the add-on may be
    mid-write, and a `file:…?mode=ro` read of a live database can still see a torn page.
    The join is on the **lemma alone** — see the module docstring.
    """
    path = os.path.join(profile, DB)
    if not os.path.exists(path):
        raise UserError(f"No AnkiMorphs database at {path} — set GIGAKU_ANKI_PROFILE_DIR.")
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, DB)
        try:
            source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(copy)
                source.backup(destination)
                destination.close()
            finally:
                source.close()
            conn = sqlite3.connect(copy)
            try:
                known = {row[0] for row in conn.execute(
                    "SELECT DISTINCT lemma FROM Morphs "
                    "WHERE highest_lemma_learning_interval >= ?", (threshold,))}
                cards = {lemma: [] for lemma in known}
                for lemma, card in conn.execute(
                        "SELECT DISTINCT morph_lemma, card_id FROM Card_Morph_Map"):
                    if lemma in known:
                        cards[lemma].append(card)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise UserError(f"Could not read the AnkiMorphs database: {exc}")
    return cards


# A `mod` past this is milliseconds, not seconds. Measured: AnkiConnect returns both —
# most cards answer in seconds and some come back ×1000, which `fromtimestamp` reads as the
# year 58529 and raises on. A seconds timestamp does not reach 1e11 until the year 5138.
MILLIS = 10 ** 11


def _when(stamp):
    """A card's `mod` as a datetime, whichever unit Anki happened to answer in."""
    try:
        return dt.datetime.fromtimestamp(stamp / 1000 if stamp > MILLIS else stamp)
    except (OverflowError, OSError, ValueError):
        return None  # a timestamp no calendar can hold dates nothing


def _evidence_cards(manual_tag):
    """The cards the user actually touched: marked known by hand, or answered.

    305 of 462,487, measured. This is the *date* rule, not a gate — a morph that appears on
    none of them is still a word, it just has no date anybody recorded.
    """
    from lib.anki import connect

    # `tag:` matters: without the prefix this searches for the tag's *text* and matches
    # nothing, which silently left only the 16 answered cards as evidence and sent a
    # thousand datable morphs to UNKNOWN_DATE. Measured and fixed the same day.
    return set(connect.call("findCards", query=f'"tag:{manual_tag}" OR -is:new') or [])


def _card_dates(card_ids):
    """{card id: when Anki last changed it}, in chunks — this can be tens of thousands."""
    from lib.anki import connect

    out = {}
    card_ids = sorted(card_ids)
    for start in range(0, len(card_ids), connect.READ_CHUNK):
        for card in connect.call("cardsInfo",
                                 cards=card_ids[start:start + connect.READ_CHUNK]):
            when = _when(card["mod"]) if card.get("mod") else None
            if when:
                out[card["cardId"]] = when
    return out


COLLECTION = "collection.anki2"
# AnkiConnect's `"tag:X" OR -is:new` in SQL. Two halves, each verified against AnkiConnect's
# own answer on the live collection (2026-08-15): `is:new` is `cards.type = 0` (16 cards
# answered ever, and `queue != 0` returns the same 16), and a note's tags are stored
# space-delimited *and space-padded* — " a b " — so a padded `instr` is the exact match for
# one whole tag (301 cards). Union: 309, identical set to AnkiConnect's 309.
#
# `instr`, not `LIKE`: the tag is `_card-status::i+0-manually` and `_` is LIKE's
# single-character wildcard, so a LIKE pattern would quietly match tags nobody wrote.
_EVIDENCE_SQL = ("SELECT c.id, c.mod FROM cards c JOIN notes n ON n.id = c.nid "
                 "WHERE c.type != 0 OR instr(n.tags, ?) > 0")


def _collection_dates(profile, manual_tag, card_ids):
    """The same `{card id: when}`, read straight out of `collection.anki2`.

    Why this exists: AnkiConnect lives *inside* Anki, so the nightly `gigaku backup` used to
    be wrapped in an `open -jga Anki` — the collection was opened every night at 00:05 for
    one query. Nothing else in a backup needs Anki running: `ankimorphs.db` is the add-on's
    own file, the known-morphs CSVs are files, Migaku is Chrome's IndexedDB. Only the dates
    came over the wire, and they are two columns of a SQLite database sitting on disk.

    When this runs, Anki is not running — that is what made AnkiConnect unreachable — which
    is exactly the condition under which the collection is safely readable (CLAUDE.md's rule
    is about a *running* Anki owning the file). Even so it is opened `mode=ro` and never
    written, and a failure returns `{}` rather than raising: an undated morph is a word this
    module already knows how to emit (`UNKNOWN_DATE`), so the worst case is the one that
    happens most nights anyway. Measured: 0.08s for the whole query against a 199 MB
    collection — the SQLite backup-API copy `_known_lemmas` makes for `ankimorphs.db` costs
    77s here and buys nothing, because nothing is writing.
    """
    path = os.path.join(profile, COLLECTION)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(_EVIDENCE_SQL, (f" {manual_tag} ",)).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        note(f"AnkiMorphs: no card dates from {COLLECTION} ({exc}) — new morphs stay undated.")
        return {}
    dated = {card: _when(mod) for card, mod in rows if card in card_ids}
    return {card: when for card, when in dated.items() if when}


def _evidence_dates(card_ids, manual_tag, profile):
    """`{card id: when}` for the touched cards among `card_ids` — over the wire, or off disk.

    Asked at all only when there is something to date. Most nights there is not: the run
    emits no new morph (measured over the week to 2026-08-15 — one night with two, both
    undatable), and asking anyway is what made a word-cache backup depend on a GUI.
    """
    if not card_ids:
        return {}
    try:
        return _card_dates(card_ids & _evidence_cards(manual_tag))
    except UserError as exc:
        note(f"AnkiMorphs: {exc}")
        return _collection_dates(profile, manual_tag, card_ids)


def read_known(langs=(), known=None, profile=""):
    """`(words, asserted)` — new words per configured language, and every (lemma, lang)
    Anki calls known.

    `langs` defaults to `settings.ankimorphs_langs()`; each known lemma is assigned to the
    configured language whose script claims it (`_lang_of`), and a lemma no configured
    language claims is skipped and counted, never guessed at.

    `known` is `{lang: words the cache already has}`: this source adds, it never re-dates —
    see the module docstring.

    `asserted` is the *whole* known set, not just what was emitted, and it is what stops
    Migaku's SKIPPED from deleting these words (`words.merge`'s `keep`). It has to be the
    whole set precisely because the words at risk are the ones already in the cache, which
    are the ones this function does not emit.
    """
    langs = tuple(langs) or tuple(settings.ankimorphs_langs())
    known = known or {}
    profile = profile or settings.ANKI_PROFILE_DIR
    config = _profile_settings(profile)
    threshold = config.get("interval_for_known_morphs", DEFAULT_INTERVAL)

    lemmas = _known_lemmas(profile, threshold)
    # lang → {cache-form: cards}; two db lemmas that lower to one German form (Abend from
    # a card, abend from the seed CSV) merge into one word carrying both cards' dates.
    by_lang, unclaimed = {lang: {} for lang in langs}, []
    for lemma, cards in lemmas.items():
        lang = _lang_of(lemma)
        if lang in by_lang:
            form = _CACHE_FORM.get(lang, str)(lemma)
            by_lang[lang].setdefault(form, []).extend(cards)
        else:
            unclaimed.append(lemma)

    # Narrowed *before* the cards are priced: only the morphs that can still become words
    # need timestamps, which is what keeps a normal run to a handful of round trips instead
    # of asking Anki about all 460k cards in the collection.
    wanted = {form: cards
              for lang, morphs in by_lang.items()
              for form, cards in morphs.items() if form not in known.get(lang, ())}
    dated = _evidence_dates({card for cards in wanted.values() for card in cards},
                            config.get("tag_known_manually") or DEFAULT_MANUAL_TAG, profile)

    words = []
    for lang in langs:
        new, undated = [], []
        for form in by_lang[lang]:
            if form not in wanted:
                continue
            when = min((dated[card] for card in wanted[form] if card in dated), default=None)
            if when is None:
                when = UNKNOWN_DATE
                undated.append(form)
            new.append(Word(form, lang, Stage.KNOWN, when))
        words += new
        # The count Anki shows is the whole table; per language it is this script's share
        # of it — so the note says both, and a gap is visible on the line rather than
        # needing to be gone looking for.
        cached = len(by_lang[lang].keys() & set(known.get(lang, ())))
        note(f"Read {len(new):,} new {lang} word(s) from AnkiMorphs "
             f"({len(by_lang[lang]):,} known lemmas, {cached:,} already cached"
             + (f", {len(undated):,} undated" if undated else "") + ").")
    if unclaimed:
        sample = ", ".join(sorted(unclaimed)[:5])
        note(f"Skipped {len(unclaimed):,} AnkiMorphs lemma(s) no configured language "
             f"claims ({sample}).")

    asserted = {(form, lang) for lang, morphs in by_lang.items() for form in morphs}
    return words, asserted
