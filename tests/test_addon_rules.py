"""The add-on's pure rules, pinned against their lib/anki mirrors.

"One rule, two implementations" used to be enforced by re-implementing the rule inside a
test; now both sides are imported and compared, so drift fails here instead of silently
scrambling the queue. The trimmer's end-cut ladder is pinned by replaying the measured
clip shapes as synthetic chunk lists.
"""
from gigaku import queries, rules, trim_rules

from lib.anki import select, store


# ── the sort key: add-on ≡ lib/anki/store ────────────────────────────────────


def test_sort_key_matches_the_cli_mirror_exactly():
    cases = [(12, "初心", 0, 10), (9, "傘", 90, 4), (1, "明後日", 100, 27), (300, "x", 55, 0)]
    for n, morph, clarity, count in cases:
        assert rules.sort_key(n, morph, clarity, count) == store.sort_key(n, morph, clarity, count)


def test_pads_match_the_cli_mirror():
    assert (rules.COUNTER_PAD, rules.CLARITY_PAD, rules.ALLCOUNT_PAD) == (
        store.COUNTER_PAD, store.CLARITY_PAD, store.ALLCOUNT_PAD)


def test_legacy_three_part_key_for_a_collection_with_no_clarity_field():
    assert rules.sort_key(12, "傘", None, 7) == "000012 傘 0007"


# ── whole_sentence: add-on ≡ lib/anki/select ─────────────────────────────────


SENTENCES = [
    "ギターを弾いたら子どもが寝てしまった。",
    "あの、歌じゃなくてギターを弾いてたけど、",
    "そこはもうパスタの専門店ですから。",
    "多分美しさとかそういうことだと思いますね",
    "食べます",
    "食べたり",
    "いいね",
    "",
    None,
]


def test_whole_sentence_agrees_with_the_cli_mirror_on_the_corpus_shapes():
    for text in SENTENCES:
        assert rules.whole_sentence(text) == select.whole_sentence(text), repr(text)


# ── norm_morph: every shape MvJ writes ───────────────────────────────────────


def test_norm_morph_handles_all_four_mvj_shapes():
    assert rules.norm_morph("撮る") == "撮る"
    assert rules.norm_morph("Sentence: 撮る") == "撮る"
    assert rules.norm_morph("Definition: 写す") == "写す"
    # The composite is the shape that used to slip through as its own word.
    assert rules.norm_morph("Sentence: 撮る | Definition: 写す") == "撮る"
    assert rules.norm_morph("  ") == ""
    assert rules.norm_morph(None) == ""


def test_norm_morph_matches_the_cli_mirror():
    shapes = ["撮る", "Sentence: 撮る", "Definition: 写す",
              "Sentence: 撮る | Definition: 写す", "", None,
              "Leben", "Sentence: Leben | Definition: Wesen"]  # German rides the same rule
    for raw in shapes:
        assert rules.norm_morph(raw) == select.norm_morph(raw), repr(raw)
    assert rules.norm_morph("Sentence: Leben | Definition: Wesen") == "Leben"


# ── the my-learn pick ────────────────────────────────────────────────────────


CLAUSE = "あの、歌じゃなくてギターを弾いてたけど、"
SENTENCE = "ギターを弾いたら子どもが寝てしまった。"


def test_the_highest_clarity_takes_the_tag_sentence_or_not():
    """The user's rule since 2026-08: the tag sits on the top-clarity card, full stop —
    a finished sentence a few points below does not take it (that tiebreak existed once
    and was removed; a tag off the top read as wrong in the alternates view)."""
    cards = [(80, 5, 1, CLAUSE), (76, 3, 2, SENTENCE)]
    assert rules.learn_pick(cards) == 1


def test_clarity_wins_no_matter_the_sentence_or_the_length():
    cards = [(80, 5, 1, CLAUSE), (70, 99, 2, SENTENCE)]
    assert rules.learn_pick(cards) == 1


def test_an_unscored_word_keeps_the_old_length_rule_untouched():
    # top == 0 → the sentence test is skipped even though card 2 finishes.
    cards = [(0, 27, 1, CLAUSE), (0, 4, 2, SENTENCE)]
    assert rules.learn_pick(cards) == 1


def test_full_ties_fall_to_the_lowest_note_id():
    cards = [(50, 5, 7, SENTENCE), (50, 5, 3, SENTENCE)]
    assert rules.learn_pick(cards) == 3


def test_learn_pick_agrees_with_the_cli_mirror():
    """Same scenario through both implementations: rules.learn_pick (the add-on's rule over
    tuples) and select.learn_winners (the CLI's rule over Cards + a score map)."""
    scenarios = [
        [(80, 5, 1, CLAUSE), (76, 3, 2, SENTENCE)],
        [(80, 5, 1, CLAUSE), (70, 99, 2, SENTENCE)],
        [(0, 27, 1, CLAUSE), (0, 4, 2, SENTENCE)],
        [(50, 5, 7, SENTENCE), (50, 5, 3, SENTENCE)],
    ]
    for cards in scenarios:
        lib_cards = [
            select.Card(note_id=nid, sentence=s, morph="傘", all_count=count)
            for clarity, count, nid, s in cards
        ]
        scores = {c.key: clarity for c, (clarity, *_rest) in zip(lib_cards, cards)}
        (winner,) = select.learn_winners(lib_cards, scores)
        assert rules.learn_pick(cards) == winner, cards


# ── the case seam: grouping must agree with Anki's case-insensitive search ───


def test_fold_matches_the_cli_mirror_and_spares_the_eszett():
    for word in ("Leben", "leben", "Straße", "STRASSE", "Größe", "傘", "", None):
        assert rules.fold(word) == select.fold(word), repr(word)
    assert rules.fold("Straße") == "straße"  # lower, never casefold — ß must survive
    assert rules.fold("傘") == "傘"           # Japanese: fold is the identity


def test_sort_key_folds_the_morph_so_one_word_sorts_as_one_word():
    """Anki compares the key as a string: with case intact every Leben card ordered
    ahead of every leben card BEFORE clarity got a say — and to the case-insensitive
    L view they are one word, whose inner order clarity exists to decide."""
    high, low = rules.sort_key(5, "Leben", 90, 7), rules.sort_key(5, "leben", 10, 7)
    assert sorted([low, high]) == [low, high]  # clarity now decides, not the L/l byte
    assert rules.sort_key(5, "Leben", 90, 7) == store.sort_key(5, "Leben", 90, 7)


def test_learn_winners_picks_one_card_for_a_word_anki_shows_as_one():
    """16 German words carried TWO my-learn tags each (Leben/leben, measured 2026-08-08):
    the grouping was case-sensitive while the L view is not. One folded group, one tag."""
    cards = [
        select.Card(note_id=1, sentence="Das Leben ist schön.", morph="Leben", all_count=5),
        select.Card(note_id=2, sentence="Wir leben hier.", morph="leben", all_count=9),
    ]
    winners = select.learn_winners(cards, {cards[0].key: 80, cards[1].key: 60})
    assert winners == {1}  # one word → one winner, the higher clarity


def test_by_word_groups_the_way_l_shows():
    cards = [
        select.Card(note_id=1, sentence="x", morph="Emoji", all_count=3),
        select.Card(note_id=2, sentence="y", morph="emoji", all_count=2),
    ]
    groups = select.by_word(cards)
    assert set(groups) == {"emoji"} and len(groups["emoji"]) == 2


def test_a_folded_word_is_one_prompt_block():
    from lib.anki import prompt

    text = prompt.render([(1, "Das Leben ist schön.", "Leben"),
                          (2, "Wir leben hier.", "leben")])
    assert "in 1 block" in text  # not the two half-comparisons the block rule forbids


def test_studying_membership_folds_too():
    cards = [select.Card(note_id=1, sentence="x", morph="Leben", all_count=5)]
    assert select.learn_winners(cards, {}, studying={"leben"}) == set()


# ── queries ──────────────────────────────────────────────────────────────────


def test_alt_query_round_trips_through_its_recogniser():
    q = queries.alt_query("🇯🇵 MvJ", "am-study-morphs", "撮る")
    assert queries.is_alt_query("🇯🇵 MvJ", "am-study-morphs", q)
    assert not queries.is_alt_query("🇯🇵 MvJ", "am-study-morphs", 'tag:"my-learn"')


def test_alt_query_covers_every_shape_mvj_writes():
    q = queries.alt_query("N", "f", "撮る")
    assert '"f:撮る"' in q
    assert '"f:Sentence: 撮る"' in q
    assert '"f:Sentence: 撮る |*"' in q  # the 💣 composite


def test_alt_query_is_exact_never_substring():
    """The undo of M gathers by alt_query too (keys._known_group since 2026-08-08) — the
    substring shape it replaced gathered 1,499 notes for `sein` with 327 of them the word,
    and 1,488 for `er` with none. Exact field shapes only; Anki's own case-insensitivity
    is what makes sein/Sein one word, not a wildcard."""
    q = queries.alt_query("🇩🇪 German", "am-study-morphs", "sein")
    assert "*sein*" not in q
    assert '"am-study-morphs:sein"' in q
    assert '"am-study-morphs:Sentence: sein"' in q


def test_search_escaping_survives_quotes_and_backslashes():
    q = queries.alt_query("N", "f", 'a"b\\c')
    assert 'a\\"b\\\\c' in q


def test_saved_search_reseed_guard_recognises_the_v1_shape_only():
    """The v2 reseed updates the sidebar's `my-learn` ONLY while it still canon-equals
    what v1 wrote — the unscoped home_query, i.e. MvJ's browser query verbatim. A row
    the user edited must never match (a deletion or edit is a decision, never fought)."""
    v1 = queries.home_query("tag:my-learn", "_card-status::i+1")
    assert queries.canon("tag:my-learn") == queries.canon(v1)
    assert queries.canon('note:"🇯🇵 MvJ" (tag:my-learn)') != queries.canon(v1)
    assert queries.canon("deck:Study tag:my-learn") != queries.canon(v1)


def test_context_query_recognised_by_the_field_mvj_configured():
    assert queries.is_context_query("Sentence Audio", '"Sentence Audio:*Show_S01E01*"')
    assert not queries.is_context_query("Sentence Audio", 'tag:"_card-status::i+1"')


def test_context_query_recognised_in_both_its_generations():
    """O is notetype-scoped since 2026-08-08 (a German and a Japanese rip of one show
    share the filename prefix); the recogniser must take the scoped shape AND the
    legacy bare one — a view opened before the change can outlive it."""
    scoped = 'note:"🇩🇪 German" "Sentence Audio:*Show_S01E01*"'
    requoted = '"note:🇩🇪 German" "Sentence Audio:*Show_S01E01*"'  # Anki's re-quoting
    assert queries.is_context_query("Sentence Audio", scoped)
    assert queries.is_context_query("Sentence Audio", requoted)
    assert not queries.is_context_query("Sentence Audio", 'note:"🇩🇪 German" (tag:my-learn)')


def test_home_query_falls_back_to_the_ready_tag():
    assert queries.home_query("deck:current", "i+1") == "deck:current"
    assert queries.home_query("", "i+1") == "tag:i+1"
    assert queries.home_query("   ", "i+1") == "tag:i+1"


def test_home_query_scopes_to_a_note_type():
    # my-learn and the status tags are collection-wide, so per-language queues exist only
    # by the note-type clause — J is Japanese, K is German, and neither shows the other.
    assert (queries.home_query("tag:my-learn", "i+1", "🇯🇵 MvJ")
            == 'note:"🇯🇵 MvJ" (tag:my-learn)')
    assert (queries.home_query("", "i+1", "🇩🇪 German")
            == 'note:"🇩🇪 German" (tag:i+1)')


def test_stale_learn_query_is_tagged_but_no_longer_ready():
    q = queries.stale_learn_query("N", "my-learn", "i+1")
    assert 'tag:"my-learn"' in q and '-tag:"i+1"' in q


# ── the trimmer's end-cut ladder ─────────────────────────────────────────────


def speech_from(by_rung, default):
    """A fake detector: chunks per min_silence threshold, `default` for unlisted ones."""
    return lambda ms: by_rung.get(ms, default)


def test_a_trailing_word_is_cut_only_when_the_clip_stops_on_it():
    """The user's rule: what is offered is decided by how the clip *ends*. A word that runs
    to the end of the track is the thing left to cut; a pause after it means the pause is."""
    flush = speech_from({}, [(0, 2000), (2400, 2700)])
    cut, reason = trim_rules.end_cut(flush, 2750)
    assert cut == 2250  # end of kept speech + the 250ms pad, short of the word's start
    assert "300ms word" in reason and "400ms pause" in reason


def test_a_pause_after_the_trailing_word_is_what_gets_cut():
    """Same clip, a second of silence after the word: the silence is the obvious waste, and
    taking the word as well is a bigger decision than a preset should make."""
    speech = speech_from({}, [(0, 2000), (2400, 2700)])
    cut, reason = trim_rules.end_cut(speech, 3700)
    assert cut == 2950  # the word is kept, the pause trimmed to the pad
    assert reason == "1000ms of trailing silence"


def test_a_word_running_into_the_clip_end_is_found_by_a_finer_rung():
    """The measured complaint: at 350ms the pause in front of the last word merges away and
    a single pass offers nothing — the ladder walks down and finds it."""
    speech = speech_from({350: [(0, 2700)]}, [(0, 2000), (2300, 2700)])
    cut, reason = trim_rules.end_cut(speech, 2750)
    assert cut == 2250
    assert "400ms word" in reason


def test_the_opening_blip_clip_is_refused_not_gutted():
    """One 2.5s clip opens with a 140ms blip, a pause, then the whole sentence; "cut what
    follows the last pause" would keep only the blip. The bulk guard declines, the tail
    path finds nothing worth cutting, and the marker is left alone."""
    chunks = [(0, 140), (600, 2400)]
    assert trim_rules.end_cut(speech_from({}, chunks), 2500) is None


def test_the_pad_never_reaches_into_the_trailing_word():
    speech = speech_from({}, [(0, 2000), (2100, 2600)])
    cut, _ = trim_rules.end_cut(speech, 2650)
    assert cut == 2100  # capped at the word's start, not keep_until + pad


def test_plain_trailing_silence_is_measured_by_the_fine_pass():
    speech = speech_from({200: [(0, 1500)]}, [(0, 1800)])
    cut, reason = trim_rules.end_cut(speech, 2500)
    assert cut == 1750 and reason == "1000ms of trailing silence"


def test_a_cut_worth_less_than_the_minimum_gain_is_not_offered():
    speech = speech_from({}, [(0, 2400)])
    assert trim_rules.end_cut(speech, 2500) is None


def test_recognisers_survive_ankis_requoting():
    """Anki round-trips `Browser(search=…)` through its parser and re-quotes whole terms:
    note:"🇯🇵 MvJ" (tag:my-learn) comes back as "note:🇯🇵 MvJ" (tag:my-learn). The day J
    grew a note clause, the byte-compare called the queue view foreign and the stash
    overwrote the user's columns — where the quotes sit must never matter."""
    built = queries.home_query("tag:my-learn", "i+1", "🇯🇵 MvJ")
    requoted = '"note:🇯🇵 MvJ" (tag:my-learn)'
    assert queries.canon(built) == queries.canon(requoted)
    assert queries.is_alt_query(
        "🇯🇵 MvJ", "am-study-morphs",
        '"note:🇯🇵 MvJ" ("am-study-morphs:撮る" OR "am-study-morphs:Sentence: 撮る")')
    assert queries.is_context_query("Sentence Audio", '"Sentence Audio:*someshow_z_33*"')


def test_the_script_classifier_mirrors_the_cli_side():
    """One rule, two implementations (the add-on cannot import lib at Anki runtime):
    rules.lang_of must agree with lib/vocab/ankimorphs.py's _lang_of, ranges and all."""
    from lib.vocab import ankimorphs as vocab_am
    assert rules.SCRIPT_RANGES == vocab_am._SCRIPT_RANGES
    assert rules.SCRIPT_ORDER == vocab_am._SCRIPT_ORDER
    for lemma in ("学ぶ", "Größe", "aufstehen", "Ｔシャツ", "ｋｗｓｋ", "кот", "...", "、"):
        assert rules.lang_of(lemma) == vocab_am._lang_of(lemma), lemma


# ── recalc language scope ────────────────────────────────────────────────────

JA_NT, DE_NT = "🇯🇵 MvJ", "🇩🇪 German"


def test_recalc_selection_scopes_only_when_exactly_one_language_is_ticked():
    assert rules.recalc_selection(True, False) == {"ja"}
    assert rules.recalc_selection(False, True) == {"de"}
    # Both ticked is the everything-recalc; both unticked is a mistake, not an empty
    # recalc — an empty read list would still wipe ankimorphs.db.
    assert rules.recalc_selection(True, True) is None
    assert rules.recalc_selection(False, False) is None


def test_recalc_keeps_drops_only_the_paused_languages_rows():
    de_only = {"de"}
    assert rules.recalc_keeps(DE_NT, de_only, JA_NT, DE_NT)
    assert not rules.recalc_keeps(JA_NT, de_only, JA_NT, DE_NT)
    ja_only = {"ja"}
    assert rules.recalc_keeps(JA_NT, ja_only, JA_NT, DE_NT)
    assert not rules.recalc_keeps(DE_NT, ja_only, JA_NT, DE_NT)


def test_recalc_keeps_never_touches_a_row_of_neither_language():
    assert rules.recalc_keeps("Basic", {"de"}, JA_NT, DE_NT)
    assert rules.recalc_keeps("Basic", {"ja"}, JA_NT, DE_NT)
    assert rules.recalc_keeps(None, {"de"}, JA_NT, DE_NT)
