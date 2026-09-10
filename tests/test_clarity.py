"""Pure logic behind `gigaku clarity` — selection, parsing, the cache, and the sort key.

The sort-key tests are the load-bearing ones. That format lives in the add-on, and these
tests build keys with the *add-on's own* implementation (`gigaku.rules`, importable since
the pure-module split) — so the properties pinned here — clarity after the morph, unscored
below scored, my-learn ordering unchanged — hold for the real writer, not a re-typed copy
of it. Get the component order wrong and everything still "works", it just silently sorts
by the wrong thing.
"""
import json
import re

import pytest
from gigaku import rules

from lib.anki import connect, prompt, select, store
from lib.config import UserError


def sort_key(counter, morph, clarity=None, all_count=0):
    return rules.sort_key(counter, morph, clarity, all_count)


def card(note_id, morph, all_count, sentence=None, clarity=""):
    return select.Card(
        note_id=note_id,
        sentence=sentence or f"文{note_id}",
        morph=morph,
        all_count=all_count,
        clarity=clarity,
    )


# ── the sort key ─────────────────────────────────────────────────────────────────────

def test_clarity_never_reorders_my_learn_cards():
    """my-learn morphs are unique, so the morph settles the order before clarity is read."""
    a = sort_key(12, "初心", clarity=0, all_count=10)
    b = sort_key(12, "明後日", clarity=100, all_count=6)
    # 初心 sorts before 明後日 by codepoint, and a perfect clarity on the other card
    # must not change that.
    assert a < b
    assert (sort_key(12, "初心", clarity=100, all_count=10) < b)


def test_clarity_orders_alternates_of_the_same_word():
    """Inside L every card shares counter and morph, so clarity is what decides."""
    keys = [
        sort_key(9, "傘", clarity=90, all_count=4),
        sort_key(9, "傘", clarity=30, all_count=20),
        sort_key(9, "傘", clarity=0, all_count=27),
    ]
    # sort_descending=True, so the browser shows the reverse — most obvious first.
    assert sorted(keys, reverse=True) == keys


def test_unscored_alternates_keep_their_old_relative_order():
    """The regression that matters: before any scoring, nothing may move."""
    unscored = [
        sort_key(9, "傘", clarity=0, all_count=n) for n in (3, 7, 12)
    ]
    legacy = [sort_key(9, "傘", all_count=n) for n in (3, 7, 12)]
    assert [k.split()[-1] for k in sorted(unscored)] == [
        k.split()[-1] for k in sorted(legacy)
    ]


def test_scored_alternates_outrank_unscored():
    assert sort_key(9, "傘", clarity=10, all_count=1) > sort_key(9, "傘", clarity=0, all_count=27)


def test_counter_still_dominates_everything():
    assert sort_key(2, "傘", clarity=100, all_count=27) < sort_key(3, "傘", clarity=0, all_count=1)


def test_absent_field_falls_back_to_the_old_three_part_key():
    assert sort_key(9, "傘", clarity=None, all_count=7) == "000009 傘 0007"


# ── selection ────────────────────────────────────────────────────────────────────────

def test_singletons_are_skipped():
    cards = [card(1, "傘", 9), card(2, "初心", 8), card(3, "初心", 4)]
    chosen, words = select.select(cards, min_morphs=0, per_word=5)
    assert {c.morph for c in chosen} == {"初心"}
    assert words == 1


def test_threshold_drops_short_sentences():
    cards = [card(i, "傘", n) for i, n in enumerate([2, 4, 6, 9], start=1)]
    chosen, _ = select.select(cards, min_morphs=5, per_word=10)
    assert sorted(c.all_count for c in chosen) == [6, 9]


def test_per_word_cap_keeps_the_richest_sentences():
    cards = [card(i, "傘", n) for i, n in enumerate([3, 11, 7, 20, 5], start=1)]
    chosen, _ = select.select(cards, min_morphs=0, per_word=2)
    assert [c.all_count for c in chosen] == [20, 11]


def test_no_cap_judges_every_alternate_above_the_floor():
    """The default. Length is the floor, not a ranking: within a word the clearest card is
    the longest sentence only 34% of the time, so a top-K by length drops the best example
    exactly where a word has most to choose from."""
    cards = [card(i, "傘", n) for i, n in enumerate([3, 11, 7, 20, 9], start=1)]
    chosen, _ = select.select(cards, min_morphs=8, per_word=0)
    assert sorted(c.all_count for c in chosen) == [9, 11, 20]


def test_words_are_taken_in_study_order():
    cards = (
        [card(100 + i, "rare", 8) for i in range(2)]
        + [card(200 + i, "common", 8) for i in range(5)]
        + [card(300 + i, "mid", 8) for i in range(3)]
    )
    chosen, words = select.select(cards, min_morphs=0, per_word=1, words=2)
    assert [c.morph for c in chosen] == ["common", "mid"]
    assert words == 2


def test_a_fully_cached_word_does_not_consume_budget():
    """Otherwise a second run re-walks the head of the queue instead of advancing."""
    cards = (
        [card(100 + i, "common", 8) for i in range(5)]
        + [card(200 + i, "mid", 8) for i in range(3)]
    )
    cached = {c.key for c in cards if c.morph == "common"}
    chosen, words = select.select(cards, min_morphs=0, per_word=5, words=1, scored=cached)
    assert {c.morph for c in chosen} == {"mid"}
    assert words == 1


def test_the_highest_clarity_wins_even_over_a_finished_sentence():
    """The user's rule since 2026-08: the tag sits on the top-clarity card, full stop.
    (A finished-sentence tiebreak inside the re-ask noise band existed once and was
    removed — a tag off the top read as wrong in the alternates view.)"""
    cards = [
        card(1, "傘", 12, sentence="あの、雨が降ってるから傘を持っていったけど、"),
        card(2, "傘", 9, sentence="雨が降ってるので傘を持っていってください。"),
    ]
    scores = {cards[0].key: 70, cards[1].key: 66}
    assert select.learn_winners(cards, scores) == {1}

    scores = {cards[0].key: 80, cards[1].key: 50}
    assert select.learn_winners(cards, scores) == {1}


def test_an_unscored_word_keeps_the_old_length_rule():
    """The invariant a scoring rollout rests on: with nothing scored, nothing moves."""
    cards = [
        card(1, "傘", 12, sentence="あの、雨が降ってるから傘を持っていったけど、"),
        card(2, "傘", 9, sentence="雨が降ってるので傘を持っていってください。"),
    ]
    assert select.learn_winners(cards, {}) == {1}


def test_the_sentence_test_reads_the_end_of_the_line():
    whole = ["雨が降ってるので傘を持っていってください。", "そうなんですよね",
             "太ることを成長とは言わないでしょ。", "これを危険と思いますか。"]
    fragments = ["あの、歌じゃなくてギターを弾いてたけど、", "静かな場所を求めてた人からすれば、",
                 "そのー、短い間にある程度集中して勉強", "あのー、上達、いろいろなことが良くなった"]
    assert [s for s in whole if not select.whole_sentence(s)] == []
    assert [s for s in fragments if select.whole_sentence(s)] == []


def test_a_half_cached_word_is_sent_whole():
    """A card that joins an already-scored word must not be rated alone: its number would
    come from a comparison of one and then be compared, in the block, with numbers from a
    call it never took part in. Whole word or nothing."""
    cards = [card(i, "傘", 9) for i in range(1, 5)]
    cached = {cards[0].key, cards[1].key}
    chosen, words = select.select(cards, min_morphs=0, per_word=0, scored=cached)
    assert [c.note_id for c in chosen] == [1, 2, 3, 4]
    assert words == 1


def test_a_word_with_nothing_new_is_still_skipped():
    """The other half of the same rule — re-running must not re-bill a settled word."""
    cards = [card(i, "傘", 9) for i in range(1, 5)]
    chosen, words = select.select(cards, min_morphs=0, per_word=0,
                                  scored={c.key for c in cards})
    assert chosen == [] and words == 0


def test_a_run_number_is_only_claimed_when_there_is_work(monkeypatch, tmp_path):
    """Run #5 in the real log is a number no card carries: it was claimed up front by a
    pass that turned out to have nothing to score. The number means "these cards were
    judged by this model under this rubric", so it must not exist before a card has been."""
    from lib.anki import clarity   # imports AnkiConnect's module — kept local, like below

    path = str(tmp_path / "clarity.json")
    monkeypatch.setattr(store.config, "CLARITY_CACHE", path)
    monkeypatch.setattr(clarity, "ensure_field", lambda: None)
    monkeypatch.setattr(clarity, "_batch", lambda *a, **k: (0, {}))
    clarity.RUN["number"] = None
    clarity.main(assume_yes=True)
    assert store.read_runs(path) == {}, "a pass that scored nothing claimed a run number"


def test_stale_rubric_keys_are_the_ones_not_stamped_current():
    """`--restale` reads the stamp from the **cache**, never from My Run: the operation
    that needs the answer is the one that clears that field, and a selection that reads a
    field it is about to blank matches nothing on its second pass — measured at the cost of
    2,855 emptied cards."""
    cache = {"a": 40, "b": 55, "c": 20}
    rubrics = {"a": "new", "b": "old"}   # "c" predates stamping
    assert store.stale(cache, rubrics, "new") == {"b", "c"}


def test_rubric_stamps_are_written_beside_the_scores(tmp_path):
    path = str(tmp_path / "clarity.json")
    store.write({"a": 40, "b": 55}, path=path, rubrics={"a": "new", "b": "new", "gone": "x"})
    assert store.read(path) == {"a": 40, "b": 55}
    # a stamp whose score is gone would outlive the thing it describes
    assert store.read_rubrics(path) == {"a": "new", "b": "new"}
    store.write({"a": 41}, path=path)
    assert store.read_rubrics(path) == {"a": "new", "b": "new"}, "stamps survive a plain write"


def test_ties_break_deterministically():
    cards = [card(2, "b", 8), card(1, "b", 8), card(4, "a", 8), card(3, "a", 8)]
    first, _ = select.select(cards, min_morphs=0, per_word=5)
    second, _ = select.select(list(reversed(cards)), min_morphs=0, per_word=5)
    assert [c.note_id for c in first] == [c.note_id for c in second]


def test_key_follows_content_not_note_id():
    assert card(1, "傘", 9, sentence="同じ").key == card(999, "傘", 3, sentence="同じ").key
    assert card(1, "傘", 9, sentence="A").key != card(1, "傘", 9, sentence="B").key


# ── parsing ──────────────────────────────────────────────────────────────────────────

def test_parse_reads_the_line_format():
    text = "1784723827132: 7\n1784723827402: 3\n"
    assert prompt.parse(text, [1784723827132, 1784723827402]) == {
        1784723827132: 7, 1784723827402: 3,
    }


def test_parse_tolerates_a_preamble_and_odd_separators():
    """Measured: the model likes to open with a heading before the pairs."""
    text = "SENTENCE ratings (id: score):\n\n1784723827132 - 7\n1784723827402\t3\n"
    assert prompt.parse(text, [1784723827132, 1784723827402]) == {
        1784723827132: 7, 1784723827402: 3,
    }


def test_parse_matches_on_id_not_position():
    text = "1784723827402: 3\n1784723827132: 8\n"
    assert prompt.parse(text, [1784723827132, 1784723827402]) == {
        1784723827132: 8, 1784723827402: 3,
    }


def test_parse_survives_a_truncated_reply():
    """A group cut off mid-answer must yield the cards it did reach, not nothing."""
    text = "1784723827132: 7\n1784723827402: 3\n178472382"
    got = prompt.parse(text, [1784723827132, 1784723827402, 1784723827999])
    assert got == {1784723827132: 7, 1784723827402: 3}


def test_parse_drops_ids_we_did_not_ask_for():
    assert prompt.parse("1784723827132: 8\n999999999999: 1\n", [1784723827132]) == {
        1784723827132: 8,
    }


def test_parse_drops_out_of_range_scores():
    text = "1784723827132: 101\n1784723827402: 0\n"
    assert prompt.parse(text, [1784723827132, 1784723827402]) == {1784723827402: 0}


def test_parse_accepts_the_full_width_of_the_scale():
    text = "1784723827132: 7\n1784723827402: 62\n1784723827999: 100\n"
    assert prompt.parse(text, [1784723827132, 1784723827402, 1784723827999]) == {
        1784723827132: 7, 1784723827402: 62, 1784723827999: 100,
    }


def test_parse_still_accepts_json():
    text = json.dumps({"scores": [{"id": 11, "score": 8}, {"id": 99, "score": 1}]})
    assert prompt.parse(text, [11]) == {11: 8}


def item(note_id, word):
    return (note_id, f"文{note_id}", word)


def test_groups_lose_nothing():
    items = [item(i, f"w{i // 4}") for i in range(23)]
    grouped = prompt.groups(items, 10)
    assert [i for g in grouped for i in g] == items


def test_a_word_is_never_split_across_calls():
    """Its candidates are rated against each other; half in the next call is two half-
    comparisons."""
    items = ([item(i, "a") for i in range(149)]
             + [item(200 + i, "b") for i in range(260)]
             + [item(600 + i, "c") for i in range(30)])
    for group in prompt.groups(items, 200):
        for word in {i[2] for i in group}:
            assert sum(1 for i in group if i[2] == word) == sum(
                1 for i in items if i[2] == word)


def test_two_whole_words_are_not_run_together_past_the_size():
    """The regression that killed a run: the old code flushed only at a word boundary and
    only once already past `size`, so 149 + 260 became one 409-card group."""
    items = [item(i, "a") for i in range(149)] + [item(200 + i, "b") for i in range(60)]
    assert [len(g) for g in prompt.groups(items, 200)] == [149, 60]


def test_words_that_fit_together_share_a_call():
    items = [item(i, "a") for i in range(120)] + [item(200 + i, "b") for i in range(60)]
    assert [len(g) for g in prompt.groups(items, 200)] == [180]


def test_an_oversized_word_is_cut_by_sentence_length_not_split():
    """A word bigger than a call gets its shortest sentences dropped — they are the ones
    that essentially never win (1 winner in 158 under 4 morphs)."""
    group = [card(i, "傘", all_count=n) for i, n in enumerate(range(1, 21), start=1)]
    kept = select.candidates(group, min_morphs=0, per_word=0, ceiling=5)
    assert [c.all_count for c in kept] == [20, 19, 18, 17, 16]


def test_the_rubric_does_not_re_grow_the_pick_line():
    """A `BEST: <id>` line per block was tried and measured out again: the model named its
    own top-scoring card in every one of 58 blocks, so the override never fired, and head
    to head over 18 words the rubric with the line and the one without kept the same winner
    14/18 each. Re-adding it costs output tokens per block and decides nothing."""
    assert "BEST" not in prompt.SYSTEM
    assert not hasattr(prompt, "promote")


def test_the_group_states_its_own_size():
    """A reply that comes back short is otherwise only detectable by counting it
    afterwards, and the model cannot count what it was never told."""
    text = prompt.render([item(1, "傘"), item(2, "傘"), item(3, "初心")])
    assert "3 items" in text and "2 blocks" in text
    assert "(2 sentences)" in text and "(1 sentence)" in text


def test_the_rubric_anchors_on_cards_that_can_actually_occur():
    """Three of the old low anchors were 3-5 morph fragments — a population the floor
    filters out before the model sees it, so the bottom of the scale was anchored on cards
    that cannot arrive while the real bottom (long, fluent, empty) had no anchor at all."""
    for gone in ("それはちょっと難しいですね", "まあそれが基本なんですよね", "手のひらを"):
        assert gone not in prompt.SYSTEM


def test_render_blocks_by_word():
    text = prompt.render([item(1, "傘"), item(2, "傘"), item(3, "初心")])
    assert text.count("WORD: 傘") == 1 and text.count("WORD: 初心") == 1
    assert text.index("WORD: 傘") < text.index("ID: 2") < text.index("WORD: 初心")


def test_the_rubric_anchors_both_ends_of_the_scale():
    """The worked examples carry the judgement, and the *low* ones carry most of it: the
    failure this rubric was rewritten to fix was empty sentences scoring 0.86 because they
    happened to be the best line of a weak block."""
    scores = [int(m) for m in re.findall(r"^SCORE: (\d+)", prompt.SYSTEM, re.M)]
    assert len(scores) >= 10
    assert max(scores) >= 90, "nothing anchors the top of the scale"
    assert min(scores) <= 10, "nothing anchors the bottom — the end that drifts"
    assert sum(1 for s in scores if s <= 25) >= 3


def test_the_rubric_does_not_re_grow_the_conflicting_rules():
    """Three rules pulling against each other swung identical re-runs by 30-40 points.
    Obviousness already presupposes the sentence has something in it, so a separate
    fragment penalty is redundant as well as destabilising."""
    for word in ("unfinished", "breaks off", "mid-thought", "cannot exceed"):
        assert word not in prompt.SYSTEM.lower()


# ── AnkiConnect write path ───────────────────────────────────────────────────────────

def test_a_failed_write_inside_multi_is_not_swallowed(monkeypatch):
    """AnkiConnect answers 200 even when an action failed — the failure is in the body.

    An unchecked `multi` therefore reports success while dropping writes, and the scores
    would be cached as written when they never landed.
    """
    def fake_call(action, **params):
        assert action == "multi"
        return [None, {"error": "cannot create note because it is a duplicate"}]

    monkeypatch.setattr(connect, "call", fake_call)
    with pytest.raises(UserError, match="duplicate"):
        connect.update_fields({1: {"My Clarity": "0.70"}, 2: {"My Clarity": "0.30"}})


def test_writes_are_chunked(monkeypatch):
    batches = []

    def fake_call(action, **params):
        batches.append(len(params["actions"]))
        return [None] * len(params["actions"])

    monkeypatch.setattr(connect, "call", fake_call)
    written = connect.update_fields({i: {"My Clarity": "0.50"} for i in range(450)})
    assert written == 450
    assert batches == [connect.WRITE_CHUNK, connect.WRITE_CHUNK, 50]


def test_no_writes_means_no_round_trip(monkeypatch):
    monkeypatch.setattr(connect, "call", lambda *a, **k: pytest.fail("should not call"))
    assert connect.update_fields({}) == 0


# ── the my-learn pick ────────────────────────────────────────────────────────────────

def learn_rank(clarity, all_count, note_id):
    """Mirrors the add-on's `best` tuple — highest wins, lowest note id breaks ties."""
    return (clarity, all_count, -note_id)


def test_my_learn_prefers_the_clearest_card():
    longest = learn_rank(0, 27, 100)
    clearest = learn_rank(90, 6, 200)
    assert clearest > longest


def test_my_learn_falls_back_to_the_old_rule_when_nothing_is_scored():
    """The safety property: an unscored collection must pick exactly what it picks today."""
    ranks = [learn_rank(0, n, nid) for n, nid in ((7, 300), (27, 100), (12, 200))]
    assert max(ranks) == learn_rank(0, 27, 100)


def test_my_learn_breaks_ties_on_lowest_note_id():
    assert max([learn_rank(50, 9, 300), learn_rank(50, 9, 100)]) == learn_rank(50, 9, 100)


# ── cache and field values ───────────────────────────────────────────────────────────

def test_field_value_spans_zero_to_one():
    assert store.field_value(0) == "0.00"
    assert store.field_value(7) == "0.07"
    assert store.field_value(70) == "0.70"
    assert store.field_value(100) == "1.00"


def test_updates_only_touch_changed_fields():
    unchanged = card(1, "傘", 9, clarity="0.70")
    changed = card(2, "傘", 9, sentence="別", clarity="0.20")
    scores = {unchanged.key: 70, changed.key: 90}
    out = store.updates([unchanged, changed], scores, clarity_field="My Clarity")
    assert out == {2: {"My Clarity": "0.90"}}


def test_an_old_cache_is_rescaled_not_rejected(tmp_path):
    """The scale went 0..10 -> 0..100 so a word's candidates could be told apart. Ten
    thousand already-judged cards had to survive that, and x10 is what they meant."""
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({"version": 1, "scores": {"abc": 7, "def": 10}}))
    assert store.read(path=str(path)) == {"abc": 70, "def": 100}


def test_a_current_cache_is_left_alone(tmp_path):
    path = tmp_path / "v2.json"
    path.write_text(json.dumps({"version": store.VERSION, "scores": {"abc": 7}}))
    assert store.read(path=str(path)) == {"abc": 7}


def test_updates_skip_unscored_cards():
    assert store.updates([card(1, "傘", 9)], {}, clarity_field="My Clarity") == {}


def test_the_sort_key_matches_the_add_ons_format():
    """Two writers, one format — the add-on rewrites this on every recalc, so a drift here
    shows up as a value that flips back and forth. Pinned against the same builder the
    add-on tests use."""
    assert store.sort_key(12, "初心", 70, 10) == sort_key(12, "初心", 70, 10)
    assert store.sort_key(12, "初心", 70, 10) == "000012 初心 070 0010"


def test_updates_refresh_the_sort_key_too():
    """Otherwise a scored card keeps a stale key until someone remembers to press R."""
    c = select.Card(note_id=1, sentence="文", morph="傘", all_count=9,
                    clarity="", alternates=12, sort="000012 傘 0009")
    out = store.updates([c], {c.key: 70}, clarity_field="My Clarity",
                        sort_field="My Sort Key")
    assert out == {1: {"My Clarity": "0.70", "My Sort Key": "000012 傘 070 0009"}}


def test_unscored_cards_get_the_four_part_key_too():
    """The mixing bug: a three-part key can't be compared against a four-part one, so a
    half-converted word sorts its alternates into nonsense."""
    old = select.Card(note_id=1, sentence="文", morph="傘", all_count=8,
                      alternates=313, sort="000313 傘 0008")
    out = store.sort_key_updates([old], {}, sort_field="My Sort Key")
    assert out == {1: {"My Sort Key": "000313 傘 000 0008"}}


def test_a_normalised_word_sorts_scored_above_unscored():
    """What the fix has to buy: one comparable order across the whole word."""
    cards = [
        select.Card(note_id=1, sentence="a", morph="傘", all_count=27, alternates=9),
        select.Card(note_id=2, sentence="b", morph="傘", all_count=4, alternates=9),
        select.Card(note_id=3, sentence="c", morph="傘", all_count=12, alternates=9),
    ]
    scores = {cards[1].key: 90}  # the short sentence is the clear one
    keys = store.sort_key_updates(cards, scores, sort_field="My Sort Key")
    ordered = sorted(cards, key=lambda c: keys[c.note_id]["My Sort Key"], reverse=True)
    assert [c.note_id for c in ordered] == [2, 1, 3]  # scored first, then by length


def test_updates_leave_the_sort_key_alone_when_it_is_already_right():
    c = select.Card(note_id=1, sentence="文", morph="傘", all_count=9,
                    clarity="0.70", alternates=12, sort="000012 傘 070 0009")
    assert store.updates([c], {c.key: 70}, clarity_field="My Clarity",
                         sort_field="My Sort Key") == {}


def test_merge_lets_a_rescore_win():
    assert store.merge({"a": 3, "b": 5}, {"a": 9}) == {"a": 9, "b": 5}


def test_cache_round_trips(tmp_path):
    path = tmp_path / "clarity.json"
    store.write({"abc": 7}, path=str(path))
    assert store.read(path=str(path)) == {"abc": 7}


def test_missing_or_corrupt_cache_reads_as_empty(tmp_path):
    assert store.read(path=str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert store.read(path=str(bad)) == {}


# ── calibration statistics ───────────────────────────────────────────────────────────

def test_stratified_sample_is_even_across_buckets_not_proportional():
    # Every morph needs ≥2 cards or it is a singleton and correctly leaves the pool.
    cards = (
        [card(i, f"w{i % 30}", 3) for i in range(1, 201)]
        + [card(500 + i, f"v{i % 5}", 9) for i in range(1, 21)]
    )
    sample = select.stratified(cards, 10)
    counts = {}
    for c in sample:
        counts[c.all_count] = counts.get(c.all_count, 0) + 1
    assert counts == {3: 10, 9: 10}


def test_stratified_sample_is_reproducible():
    cards = [card(i, f"w{i % 20}", (i % 12) + 1) for i in range(1, 300)]
    assert ([c.note_id for c in select.stratified(cards, 5)]
            == [c.note_id for c in select.stratified(cards, 5)])


def test_bucket_rows_report_the_population_and_the_sample():
    cards = [card(i, f"w{i % 5}", 4) for i in range(1, 21)]
    scores = {1: 1.0, 2: 0.0, 3: 0.8, 4: 0.2}
    rows = {r["morphs"]: r for r in select.bucket_rows(cards, scores)}
    assert rows[4]["cards"] == 20
    assert rows[4]["sampled"] == 4
    assert rows[4]["share_high"] == pytest.approx(0.5)
    assert rows[4]["mean"] == pytest.approx(0.5)


def test_recommend_picks_the_lowest_bucket_that_holds_up():
    rows = [
        {"morphs": 3, "share_high": 0.01},
        {"morphs": 4, "share_high": 0.02},
        {"morphs": 5, "share_high": 0.20},
        {"morphs": 6, "share_high": 0.35},
    ]
    assert select.recommend(rows, floor=0.05) == 5


def test_recommend_returns_none_when_nothing_clears_the_floor():
    rows = [{"morphs": n, "share_high": 0.01} for n in (3, 4, 5)]
    assert select.recommend(rows, floor=0.05) is None


def test_spearman_detects_the_premise_failing():
    identical = [(1, 0.1), (2, 0.2), (3, 0.3), (4, 0.4)]
    assert select.spearman(identical) == pytest.approx(1.0)
    inverted = [(1, 0.4), (2, 0.3), (3, 0.2), (4, 0.1)]
    assert select.spearman(inverted) == pytest.approx(-1.0)
    assert select.spearman([(1, 0.5)]) is None
    assert select.spearman([(1, 0.5), (1, 0.5), (1, 0.5)]) is None


# ── the --loop driver ────────────────────────────────────────────────────────────────

def test_loop_keeps_going_until_a_pass_scores_nothing(monkeypatch):
    """The stop condition. It lived in a throwaway shell script before, where neither it
    nor the retry policy could be tested."""
    from lib.anki import clarity
    passes = iter([300, 250, 0])
    monkeypatch.setattr(clarity, "ensure_field", lambda: None)
    monkeypatch.setattr(clarity.store, "start_run", lambda model, rubric=None, path=None: 7)
    monkeypatch.setattr(clarity.store, "read", lambda path=None: {})
    seen = []
    monkeypatch.setattr(clarity, "_batch",
                        lambda cache, **kw: (seen.append(1), (next(passes), cache))[1])
    clarity.main(assume_yes=True, loop=True)
    assert len(seen) == 3


def test_a_single_pass_does_not_loop(monkeypatch):
    from lib.anki import clarity
    monkeypatch.setattr(clarity, "ensure_field", lambda: None)
    monkeypatch.setattr(clarity.store, "start_run", lambda model, rubric=None, path=None: 7)
    monkeypatch.setattr(clarity.store, "read", lambda path=None: {})
    seen = []
    monkeypatch.setattr(clarity, "_batch",
                        lambda cache, **kw: (seen.append(1), (300, cache))[1])
    clarity.main(assume_yes=True, loop=False)
    assert len(seen) == 1


def test_loop_backs_off_and_retries_a_failed_pass(monkeypatch):
    """A failed pass is nearly always a rate limit; everything scored is already on disk,
    so the run should wait rather than surrender."""
    from lib.anki import clarity
    from lib.config import UserError
    outcomes = iter([UserError("rate limited"), UserError("again"), 120, 0])
    def fake_batch(cache, **kw):
        nxt = next(outcomes)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt, cache
    slept = []
    monkeypatch.setattr(clarity, "ensure_field", lambda: None)
    monkeypatch.setattr(clarity.store, "start_run", lambda model, rubric=None, path=None: 7)
    monkeypatch.setattr(clarity.store, "read", lambda path=None: {})
    monkeypatch.setattr(clarity, "_batch", fake_batch)
    monkeypatch.setattr(clarity.time, "sleep", lambda s: slept.append(s))
    clarity.main(assume_yes=True, loop=True)
    assert len(slept) == 2


def test_loop_gives_up_after_consecutive_failures(monkeypatch):
    from lib.anki import clarity
    from lib.config import UserError
    def always_fail(cache, **kw):
        raise UserError("still down")
    monkeypatch.setattr(clarity, "ensure_field", lambda: None)
    monkeypatch.setattr(clarity.store, "start_run", lambda model, rubric=None, path=None: 7)
    monkeypatch.setattr(clarity.store, "read", lambda path=None: {})
    monkeypatch.setattr(clarity, "_batch", always_fail)
    monkeypatch.setattr(clarity.time, "sleep", lambda s: None)
    clarity.main(assume_yes=True, loop=True)  # returns rather than raising


def test_a_cancelled_pass_is_never_retried(monkeypatch):
    """Declining the confirmation means stop, not 'try again in ten minutes'."""
    from lib.anki import clarity
    from lib.config import UserError
    def cancelled(cache, **kw):
        raise UserError("cancelled")
    monkeypatch.setattr(clarity, "ensure_field", lambda: None)
    monkeypatch.setattr(clarity.store, "start_run", lambda model, rubric=None, path=None: 7)
    monkeypatch.setattr(clarity.store, "read", lambda path=None: {})
    monkeypatch.setattr(clarity, "_batch", cancelled)
    with pytest.raises(UserError, match="cancelled"):
        clarity.main(assume_yes=False, loop=True)


@pytest.mark.parametrize("lang,notetype", [("ja", "🇯🇵 MvJ"), ("de", "🇩🇪 German")])
def test_reset_clears_only_its_own_languages_notes(monkeypatch, tmp_path, lang, notetype):
    """My Clarity is one field name across both notetypes, so an unscoped find under
    --lang de would clear all the Japanese scores while deleting only the German cache
    — reset() must carry the notetype clause like every other query in the module."""
    from lib.anki import clarity

    asked = []
    monkeypatch.setattr(clarity, "ensure_field", lambda: None)
    monkeypatch.setattr(clarity.connect, "find_notes", lambda q: asked.append(q) or [])
    monkeypatch.setattr(clarity.store, "read", lambda path=None: {})
    clarity.set_lang(lang)
    monkeypatch.setattr(clarity.P, "cache", str(tmp_path / "nope.json"), raising=False)

    try:
        clarity.reset(assume_yes=True)
    finally:
        clarity.set_lang("ja")  # P is module state — leave it as other tests expect

    assert asked == [f'note:"{notetype}" "My Clarity:_*"']


def test_each_language_has_its_own_morph_floor():
    """German measured its own floor (two --calibrate runs, 2026-08-08): the corpus
    plateaus at ~0.2 past 7 morphs — the ja 9 would drop most of its scoreable cards."""
    from lib.anki import clarity
    from lib.config import settings

    try:
        clarity.set_lang("de")
        assert clarity._min_morphs() == settings.CLARITY_MIN_MORPHS_DE == 5
        clarity.set_lang("ja")
        assert clarity._min_morphs() == settings.CLARITY_MIN_MORPHS == 9
    finally:
        clarity.set_lang("ja")


def test_each_language_binds_its_own_profile_and_cache():
    """German scores too (2026-08-07) — through its own named rubric, cache and notetype,
    never the ja ones: a shared cache would false-restale the other language, and the two
    fingerprints meeting inside one word is the exact bug the stamp exists to catch."""
    from lib.anki import clarity
    from lib.config import UserError
    clarity.set_lang("de")
    de = clarity.P
    clarity.set_lang("ja")
    ja = clarity.P
    assert ja.cache != de.cache
    assert ja.fingerprint != de.fingerprint
    assert (ja.notetype, de.notetype) == ("🇯🇵 MvJ", "🇩🇪 German")
    assert (ja.study_deck, de.study_deck) == ("Study 🇯🇵", "Study 🇩🇪")
    assert ja.field == de.field == "My Clarity"  # field names identical by design
    with pytest.raises(UserError, match="--lang must be"):
        clarity.set_lang("fr")
