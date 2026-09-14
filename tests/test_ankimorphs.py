"""AnkiMorphs as a word source — what gets in, what doesn't, and with which date.

The contract is that gigaku's known count equals the one Anki shows, so nothing is filtered
at all: every known morph becomes a word, deduplicated against the cache. Two filters were
tried and measured out — a hand-marked "evidence" card (cost: 392 real words) and
subtracting `removed-garbage.csv` (cost: the 752 that stood between 3,045 and 3,797). Both
have a regression test below, because both looked obviously right at the time.
"""
import datetime as dt
import json
import os
import sqlite3

import pytest

from lib.config import UserError
from lib.vocab import ankimorphs
from lib.vocab.words import UNKNOWN_DATE, Stage

MOD = {  # card id → mod timestamp, as AnkiConnect reports it
    10: dt.datetime(2026, 7, 22, 9, 0).timestamp(),
    11: dt.datetime(2026, 7, 28, 9, 0).timestamp(),
    12: dt.datetime(2026, 7, 30, 9, 0).timestamp(),
    13: dt.datetime(2026, 7, 24, 9, 0).timestamp(),
}


@pytest.fixture
def profile(tmp_path):
    """A profile dir shaped like the real one: settings, ankimorphs.db, the garbage CSV."""
    (tmp_path / "ankimorphs_profile_settings.json").write_text(json.dumps({
        "interval_for_known_morphs": 1,
        "tag_known_manually": "_card-status::i+0-manually",
    }), encoding="utf-8")

    known_morphs = tmp_path / "known-morphs"
    known_morphs.mkdir()
    (known_morphs / "removed-garbage.csv").write_text(
        "Morph-Lemma\nゴミ\n", encoding="utf-8")

    conn = sqlite3.connect(tmp_path / "ankimorphs.db")
    conn.executescript("""
        CREATE TABLE Morphs (lemma TEXT, inflection TEXT,
                             highest_lemma_learning_interval INTEGER,
                             highest_inflection_learning_interval INTEGER,
                             PRIMARY KEY (lemma, inflection));
        CREATE TABLE Card_Morph_Map (card_id INTEGER, morph_lemma TEXT,
                                     morph_inflection TEXT,
                                     PRIMARY KEY (card_id, morph_lemma, morph_inflection));
    """)
    conn.executemany("INSERT INTO Morphs VALUES (?,?,?,?)", [
        ("学ぶ", "学んだ", 1, 1),     # known, on two cards
        ("ゴミ", "ゴミ", 1, 1),       # known, and on the garbage list — still a word
        ("古い", "古い", 1, 1),       # known, and already in the cache
        ("紙", "紙", 1, 1),           # known, on a card nobody ever marked or answered
        ("空", "空い", 1, 1),         # known through one inflection, carded under another
        ("霧", "霧", 1, 1),           # known, on no card at all — nothing can date it
        ("まだ", "まだ", 0, 0),       # not known
    ])
    conn.executemany("INSERT INTO Card_Morph_Map VALUES (?,?,?)", [
        (11, "学ぶ", "学んだ"), (12, "学ぶ", "学んだ"),
        (10, "ゴミ", "ゴミ"),
        (10, "古い", "古い"),
        (13, "紙", "紙"),
        (12, "空", "空く"),           # the inflection here differs from the Morphs row
        (10, "まだ", "まだ"),
    ])
    conn.commit()
    conn.close()
    return str(tmp_path)


@pytest.fixture
def anki(monkeypatch):
    """Stand in for AnkiConnect. Cards 10-12 were touched by hand; 13 was not."""
    def call(action, **params):
        if action == "findCards":
            return [10, 11, 12]
        if action == "cardsInfo":
            return [{"cardId": c, "mod": MOD[c]} for c in params["cards"] if c in MOD]
        raise AssertionError(action)

    from lib.anki import connect
    monkeypatch.setattr(connect, "call", call)


def read(profile, known=(), langs=("ja", "de")):
    # Both languages configured, as live — on an all-Japanese db that must behave exactly
    # like the old single-language read, which is what keeps every test below unchanged.
    return ankimorphs.read_known(langs=langs, known={"ja": set(known)}, profile=profile)[0]


def asserted(profile, known=(), langs=("ja", "de")):
    return ankimorphs.read_known(langs=langs, known={"ja": set(known)}, profile=profile)[1]


def test_a_morph_is_dated_by_the_earliest_card_it_sits_on(profile, anki):
    # Two cards, 28 and 30 July. AnkiMorphs has no timestamp of its own, so the card is the
    # only date there is, and the earliest one is when the morph became known.
    words = read(profile)
    learnt = next(w for w in words if w.word == "学ぶ")
    assert learnt.date.date() == dt.date(2026, 7, 28)
    assert (learnt.language, learnt.stage) == ("ja", Stage.KNOWN)


def test_a_morph_is_not_required_to_have_been_marked_by_hand(profile, anki):
    # The regression: requiring a hand-marked card *as a gate* threw away 392 real words
    # (measured 2026-08-01). 紙 sits only on card 13, which nobody touched — it is still a
    # word. It just has no date, which is a different thing from not existing.
    paper = next(w for w in read(profile) if w.word == "紙")
    assert paper.date == UNKNOWN_DATE


def test_a_lemma_carded_under_another_inflection_still_counts(profile, anki):
    # The lemma is what becomes a word, so the join is on the lemma alone. Measured, this
    # costs nothing in the real collection (0 extra morphs) — it is here for correctness.
    assert "空" in {w.word for w in read(profile)}


def test_an_undated_morph_gets_the_year_1_never_today(profile, anki):
    # Today would read as a word learnt this week, and the earliest `mod` of a card nobody
    # touched is recalc noise dressed as history. The year 1 says "known, when unrecorded"
    # and can never fall inside a report week.
    fog = next(w for w in read(profile) if w.word == "霧")   # on no card at all
    assert fog.date == UNKNOWN_DATE
    assert UNKNOWN_DATE.year == 1


def test_only_a_card_the_user_touched_gives_a_real_date(profile, anki):
    # 学ぶ is on cards 11 and 12, both hand-marked: earliest wins. Everything whose only
    # cards are untouched falls through to the sentinel.
    dates = {w.word: w.date for w in read(profile)}
    assert dates["学ぶ"].date() == dt.date(2026, 7, 28)   # cards 11 and 12, earliest wins
    assert dates["空"].date() == dt.date(2026, 7, 30)     # card 12, touched
    # 紙 sits only on card 13 and 霧 on none at all; neither was ever touched.
    assert {word for word, when in dates.items() if when == UNKNOWN_DATE} == {"紙", "霧"}


def test_the_garbage_list_is_not_subtracted(profile, anki):
    # It is the user's "never show me this morph again" list, so subtracting it looked
    # right — and it was exactly the 752 morphs keeping gigaku's count below Anki's.
    # The counts agreeing wins; see the module docstring.
    assert "ゴミ" in {w.word for w in read(profile)}


def test_every_known_morph_becomes_a_word_so_the_two_counts_agree(profile, anki):
    # The whole contract in one line: what Anki calls known, gigaku knows. The only
    # absentees are the already-cached (dedup) and the undatable (named, not stamped).
    known_in_db = {"学ぶ", "ゴミ", "古い", "紙", "空", "霧"}
    assert {w.word for w in read(profile)} == known_in_db


def test_a_word_the_cache_already_dates_is_left_alone(profile, anki):
    # merge() keeps the latest timestamp, so re-emitting a word Migaku dated in November
    # with a July "set known" date would walk it forward and count it as new this week.
    assert "古い" not in {w.word for w in read(profile, known={"古い"})}
    assert "古い" in {w.word for w in read(profile)}  # …and without the cache, it is fine


def test_a_morph_below_the_threshold_is_not_known(profile, anki):
    assert "まだ" not in {w.word for w in read(profile)}


@pytest.fixture
def mixed_profile(tmp_path):
    """A db the way it looks once German note filters share the collection: no language
    column anywhere, so the lemma's script is the only witness."""
    (tmp_path / "ankimorphs_profile_settings.json").write_text(json.dumps({
        "interval_for_known_morphs": 1,
        "tag_known_manually": "_card-status::i+0-manually",
    }), encoding="utf-8")
    conn = sqlite3.connect(tmp_path / "ankimorphs.db")
    conn.executescript("""
        CREATE TABLE Morphs (lemma TEXT, inflection TEXT,
                             highest_lemma_learning_interval INTEGER,
                             highest_inflection_learning_interval INTEGER,
                             PRIMARY KEY (lemma, inflection));
        CREATE TABLE Card_Morph_Map (card_id INTEGER, morph_lemma TEXT,
                                     morph_inflection TEXT,
                                     PRIMARY KEY (card_id, morph_lemma, morph_inflection));
    """)
    conn.executemany("INSERT INTO Morphs VALUES (?,?,?,?)", [
        ("学ぶ", "学んだ", 1, 1),           # Japanese
        ("aufstehen", "stehe", 1, 1),       # German
        ("Größe", "Größe", 1, 1),           # German umlaut+ß — Latin-1 letters count
        ("Ｔシャツ", "Ｔシャツ", 1, 1),      # mixed scripts — Japanese quoting Latin
        ("ｋｗｓｋ", "ｋｗｓｋ", 1, 1),      # fullwidth netspeak — Japanese, measured
        ("１２３", "１２３", 1, 1),          # fullwidth digits — the fullwidth block is ja
        ("кот", "кот", 1, 1),               # Cyrillic — no configured language claims it
        ("...", "...", 1, 1),               # ASCII punctuation — nobody's word either
    ])
    conn.commit()
    conn.close()
    return str(tmp_path)


def test_the_script_is_the_language(mixed_profile, anki):
    words = {w.word: w.language for w in read(mixed_profile)}
    assert words["学ぶ"] == "ja"
    assert words["aufstehen"] == "de"
    assert words["größe"] == "de"          # ä ö ü ß must not fall through the classifier


def test_german_lemmas_are_lowered_to_the_caches_convention(mixed_profile, anki):
    # Migaku lowercases its German dictForms and the cache carries ten months of them, so
    # spaCy's capitalized nouns are lowered at this boundary — the cache must never grow
    # an Abend beside its abend. Japanese is untouched (no case to speak of).
    words = {w.word for w in read(mixed_profile)}
    assert "größe" in words and "Größe" not in words
    keys = asserted(mixed_profile)
    assert ("größe", "de") in keys and ("Größe", "de") not in keys
    # And the lowered form is what dedups against the cache:
    out = ankimorphs.read_known(langs=("ja", "de"), known={"de": {"größe"}},
                                profile=mixed_profile)
    assert "größe" not in {w.word for w in out[0]}
    assert ("größe", "de") in out[1]


def test_a_mixed_script_lemma_is_japanese_and_fullwidth_stays_japanese(mixed_profile, anki):
    # Ｔシャツ is Japanese quoting Latin — the reverse never happens — and the fullwidth
    # block (ｋｗｓｋ, ｒｐｇ: 7 of 3,800 known lemmas, measured 2026-08-06) is Japanese
    # netspeak, not German.
    words = {w.word: w.language for w in read(mixed_profile)}
    assert words["Ｔシャツ"] == "ja"
    assert words["ｋｗｓｋ"] == "ja"


def test_a_lemma_no_script_claims_is_skipped_never_guessed(mixed_profile, anki):
    words = {w.word for w in read(mixed_profile)}
    keys = asserted(mixed_profile)
    assert "кот" not in words and "..." not in words
    assert not any(lemma in ("кот", "...") for lemma, _ in keys)


def test_an_unconfigured_language_is_skipped_not_mislabelled(mixed_profile, anki):
    # The old failure mode this module exists to prevent: with only ja configured, a German
    # lemma must vanish from the read (with a note), never come back stamped `ja`.
    words = {w.word: w.language for w in read(mixed_profile, langs=("ja",))}
    assert "aufstehen" not in words
    assert all(lang == "ja" for lang in words.values())
    assert ("aufstehen", "de") not in asserted(mixed_profile, langs=("ja",))


def test_dedup_and_the_shield_are_per_language(mixed_profile, anki):
    # A cached German word is not re-emitted (dedup) but is still asserted (the shield
    # against Migaku's SKIPPED) — the same two-sided contract the ja side has.
    out = ankimorphs.read_known(langs=("ja", "de"), known={"de": {"aufstehen"}},
                                profile=mixed_profile)
    assert "aufstehen" not in {w.word for w in out[0]}
    assert ("aufstehen", "de") in out[1]


def test_asserted_keys_carry_the_language(mixed_profile, anki):
    keys = asserted(mixed_profile)
    assert ("学ぶ", "ja") in keys and ("aufstehen", "de") in keys


def test_a_missing_database_is_a_user_error_not_a_crash(tmp_path, anki):
    with pytest.raises(UserError):
        read(str(tmp_path))


@pytest.fixture
def anki_down(monkeypatch):
    """Anki is not running, so AnkiConnect refuses exactly as `lib/anki/connect.py` does."""
    from lib.anki import connect

    def call(action, **params):
        raise UserError("can't reach AnkiConnect at http://127.0.0.1:8765 (Connection refused)")

    monkeypatch.setattr(connect, "call", call)


@pytest.fixture
def collection(profile):
    """A `collection.anki2` carrying the same evidence as the AnkiConnect stub.

    Cards 10 and 11 are hand-marked, 12 has been answered, 13 is neither — the set the
    `anki` fixture reports as `[10, 11, 12]`. Only the columns the query names exist;
    Anki's real table is thirty wide and none of the rest is read.
    """
    conn = sqlite3.connect(os.path.join(profile, ankimorphs.COLLECTION))
    conn.executescript("""
        CREATE TABLE notes (id INTEGER PRIMARY KEY, tags TEXT);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, mod INTEGER, type INTEGER);
    """)
    # Anki stores tags space-delimited AND space-padded — " a b " — which is what makes a
    # padded `instr` an exact whole-tag match.
    conn.executemany("INSERT INTO notes VALUES (?,?)", [
        (110, " _card-status::i+0-manually "),
        (111, " marked _card-status::i+0-manually "),   # not the only tag on the note
        (112, " "),                                     # untagged; dated by being answered
        (113, " _card-status::i+0-manually-ish "),      # a longer tag that starts the same
    ])
    conn.executemany("INSERT INTO cards VALUES (?,?,?,?)", [
        (10, 110, int(MOD[10]), 0),
        (11, 111, int(MOD[11]), 0),
        (12, 112, int(MOD[12]), 2),   # answered: AnkiConnect's `-is:new` is `type != 0`
        (13, 113, int(MOD[13]), 0),   # neither marked nor answered — nothing dates it
    ])
    conn.commit()
    conn.close()
    return profile


def test_the_collection_dates_a_morph_exactly_as_ankiconnect_does(profile, collection,
                                                                  anki, anki_down):
    """The whole point: with Anki shut, the words come out identical.

    Both fixtures are requested so the two paths run over the same profile — `anki_down`
    is applied second and wins, so this read is the offline one; the expected dates are
    the ones every AnkiConnect test above asserts.
    """
    dates = {w.word: w.date for w in read(collection)}
    assert dates["学ぶ"].date() == dt.date(2026, 7, 28)   # cards 11 and 12, earliest wins
    assert dates["空"].date() == dt.date(2026, 7, 30)     # card 12, answered
    assert {word for word, when in dates.items() if when == UNKNOWN_DATE} == {"紙", "霧"}


def test_a_tag_that_merely_starts_the_same_is_not_the_tag(collection, anki_down):
    """`_` is LIKE's single-character wildcard and the tag is full of them.

    紙 sits only on card 13, whose note carries `…-manually-ish`. A LIKE pattern would
    match it and hand the morph a date the user never gave it; `instr` on the padded tag
    does not.
    """
    paper = next(w for w in read(collection) if w.word == "紙")
    assert paper.date == UNKNOWN_DATE


def test_a_quiet_night_asks_anki_nothing_at_all(profile, monkeypatch):
    """Nothing new to date → no AnkiConnect, no collection read, no reason to open Anki.

    This is most nights: measured over the week to 2026-08-15, one run emitted two morphs
    and every other emitted none. The round trip that made a word backup depend on a GUI
    was being paid to date an empty set.
    """
    from lib.anki import connect

    def refuse(action, **params):
        raise AssertionError(f"asked Anki for {action} with nothing to date")

    monkeypatch.setattr(connect, "call", refuse)
    everything = {"学ぶ", "ゴミ", "古い", "紙", "空", "霧"}
    assert read(profile, known=everything) == []


def test_no_collection_leaves_the_morphs_undated_rather_than_crashing(profile, anki_down):
    # Dating is best-effort at both ends: an undated morph is a word this module already
    # emits, so a missing or unreadable collection must never sink an import.
    words = read(profile)
    assert {w.word for w in words} == {"学ぶ", "ゴミ", "古い", "紙", "空", "霧"}
    assert {w.date for w in words} == {UNKNOWN_DATE}


def test_the_collection_is_read_only_and_only_when_ankiconnect_is_down(collection, anki,
                                                                      monkeypatch):
    opened = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        opened.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    read(collection)
    live = os.path.join(collection, ankimorphs.COLLECTION)
    # AnkiConnect answered, so the collection was not touched at all.
    assert not [path for path in opened if live in path]

    opened.clear()
    from lib.anki import connect
    monkeypatch.setattr(connect, "call",
                        lambda *a, **kw: (_ for _ in ()).throw(UserError("no AnkiConnect")))
    read(collection)
    assert f"file:{live}?mode=ro" in opened
    assert live not in opened   # never opened writable, never copied


def test_the_live_database_is_never_opened_in_place(profile, anki, monkeypatch):
    """It is copied through SQLite's backup API first — the add-on may be mid-write."""
    opened = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        opened.append(str(target))
        return real(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    read(profile)
    live = os.path.join(profile, "ankimorphs.db")
    # The live file is opened read-only, and every query runs against the copy.
    assert f"file:{live}?mode=ro" in opened
    assert live not in opened
