"""The subtitle translator: SRT round-trip, the request shape, and the id-echo contract.

Pure — `lib.claude.ask` is stubbed everywhere (tests/conftest.py refuses a real call), so
nothing here spends a subscription request. What is pinned is the part that decides whether
a Secondary track is trustworthy: a cue can only ever receive the text answered under its
own id, and a line that never comes back stops the episode instead of landing as a blank.
"""
import json

import pytest

from lib.subs import srt, translate, translate_prompt


def cue(index, text, start="00:00:01,000"):
    return srt.Cue(index, f"{start} --> 00:00:02,000", text)


def cues(count, first=1):
    return [cue(i, f"Zeile {i}") for i in range(first, first + count)]


# ── lib/subs/srt.py ──────────────────────────────────────────────────────────

def test_round_trip_is_exact():
    original = [cue(1, "Erste Zeile"), cue(2, "Zweite Zeile")]
    assert srt.parse(srt.render(original)) == original


def test_reads_crlf_and_a_bom(tmp_path):
    path = tmp_path / "x.srt"
    path.write_bytes("﻿1\r\n00:00:01,000 --> 00:00:02,000\r\nHallo\r\n\r\n".encode())
    assert srt.read(path) == [cue(1, "Hallo")]


def test_skips_a_block_that_is_not_a_cue():
    text = "not a cue\n\n7\n00:00:01,000 --> 00:00:02,000\nJa\n\nx\ny\n"
    assert [c.index for c in srt.parse(text)] == [7]


def test_write_is_atomic_and_leaves_no_debris(tmp_path):
    path = tmp_path / "out.srt"
    srt.write(path, [cue(1, "Ja")])
    assert srt.read(path) == [cue(1, "Ja")]
    assert [p.name for p in tmp_path.iterdir()] == ["out.srt"]


# ── the request ──────────────────────────────────────────────────────────────

def test_context_lines_carry_no_id():
    """A context line the model cannot name is a context line it cannot answer for."""
    view = [(cue(1, "Vorher"), False), (cue(2, "Jetzt"), True)]
    rendered = translate_prompt.render(view)
    assert "2: Jetzt" in rendered
    assert "1:" not in rendered
    assert "Vorher" in rendered  # still shown — it is why the next line makes sense


def test_the_request_states_how_many_lines_it_wants():
    view = [(c, True) for c in cues(3)]
    assert "Translate the 3 lines" in translate_prompt.render(view)


def test_groups_ask_for_every_cue_exactly_once():
    every = cues(25)
    asked = [c.index for view in srt.groups(every, 10, 3)
             for c, needed in view if needed]
    assert asked == [c.index for c in every]


def test_groups_carry_context_on_both_sides_of_a_seam():
    views = srt.groups(cues(25), 10, 3)
    middle = views[1]
    assert [c.index for c, needed in middle if not needed] == [8, 9, 10, 21, 22, 23]
    assert [c.index for c, needed in middle if needed] == list(range(11, 21))


def test_regroup_asks_only_for_the_gaps_and_keeps_the_rest_as_context():
    view = [(c, True) for c in cues(4)]
    again = srt.regroup(view, [2, 4])
    assert [c.index for c, needed in again if needed] == [2, 4]
    assert len(again) == 4  # nothing dropped — the scene is still there to read


# ── the id-echo contract ─────────────────────────────────────────────────────

def test_parse_matches_on_the_echoed_id_not_on_position():
    """The whole reason for ids: a reply that comes back short must not shift the rest."""
    got = translate_prompt.parse("3: третья\n1: первая\n", [1, 2, 3])
    assert got == {1: "первая", 3: "третья"}


def test_parse_ignores_an_id_that_was_never_asked_for():
    assert translate_prompt.parse("99: чужая\n1: своя\n", [1]) == {1: "своя"}


def test_parse_keeps_a_colon_inside_the_translation():
    assert translate_prompt.parse("5: в 20:30 вечера", [5]) == {5: "в 20:30 вечера"}


def test_parse_drops_an_empty_answer_so_it_is_re_asked():
    assert translate_prompt.parse("1:\n2: есть", [1, 2]) == {2: "есть"}


def test_parse_keeps_the_first_answer_for_a_repeated_id():
    assert translate_prompt.parse("1: первая\n1: вторая", [1]) == {1: "первая"}


def test_parse_survives_a_preamble_and_a_marker():
    text = "Here are the lines:\n\n→ 1: раз\n2: два\n\nDone!"
    assert translate_prompt.parse(text, [1, 2]) == {1: "раз", 2: "два"}


# ── lib/subs/translate.py ────────────────────────────────────────────────────

@pytest.fixture
def episode(tmp_path, monkeypatch):
    """A three-cue episode, with the work file kept inside tmp_path."""
    monkeypatch.setattr(translate, "WORK_DIR", str(tmp_path / "work"))
    primary = tmp_path / "Show" / "Show - S01E01 - Primary.srt"
    primary.parent.mkdir()
    srt.write(primary, [cue(1, "Eins", "00:00:01,000"), cue(2, "Zwei", "00:00:05,000"),
                        cue(3, "Drei", "00:00:09,000")])
    return primary, primary.with_name("Show - S01E01 - Secondary.srt")


def answering(replies):
    """A stub `claude.ask` that returns each canned reply in turn, recording the prompts."""
    sent = []

    def ask(text, _system, _model, **_kwargs):
        sent.append(text)
        return replies[len(sent) - 1], {}, 0.01

    ask.sent = sent
    return ask


def test_a_translated_episode_keeps_every_timecode(episode, monkeypatch):
    primary, secondary = episode
    monkeypatch.setattr(translate.claude, "ask",
                        answering(["1: раз\n2: два\n3: три"]))

    stats = translate.episode(primary, secondary)

    assert stats["translated"] == 3 and stats["calls"] == 1
    assert [(c.index, c.timecode, c.text) for c in srt.read(secondary)] == [
        (c.index, c.timecode, russian)
        for c, russian in zip(srt.read(primary), ["раз", "два", "три"])
    ]


def test_a_short_reply_is_re_asked_by_id_and_only_for_the_gap(episode, monkeypatch):
    primary, secondary = episode
    ask = answering(["1: раз\n3: три", "2: два"])
    monkeypatch.setattr(translate.claude, "ask", ask)

    translate.episode(primary, secondary)

    assert len(ask.sent) == 2
    # The re-ask names the missing line and demotes the answered ones to context.
    assert "2: Zwei" in ask.sent[1]
    assert "1: Eins" not in ask.sent[1] and "Eins" in ask.sent[1]
    assert {c.index: c.text for c in srt.read(secondary)} == {1: "раз", 2: "два", 3: "три"}


def test_a_line_that_never_comes_back_stops_the_episode(episode, monkeypatch):
    primary, secondary = episode
    monkeypatch.setattr(translate.claude, "ask", answering(["1: раз\n3: три"] * 3))

    with pytest.raises(translate.TranslateError, match="1 of 3 lines never came back"):
        translate.episode(primary, secondary)

    assert not secondary.exists()  # never a Secondary with a blank in it
    saved = json.loads(translate.work_path(primary).read_text(encoding="utf-8"))
    assert saved["lines"] == {"1": "раз", "3": "три"}  # what did come back is kept


def test_a_resumed_episode_pays_only_for_the_gap(episode, monkeypatch):
    primary, secondary = episode
    monkeypatch.setattr(translate.claude, "ask", answering(["1: раз\n3: три"] * 3))
    with pytest.raises(translate.TranslateError):
        translate.episode(primary, secondary)

    ask = answering(["2: два"])
    monkeypatch.setattr(translate.claude, "ask", ask)
    stats = translate.episode(primary, secondary)

    # `normalised` is 0 and `calls` is still 1 because these cues carry no joined token at all —
    # the dictionary pass has nothing to ask about and costs nothing, which is the behaviour a
    # resumed episode depends on.
    assert stats == {"cues": 3, "translated": 1, "cached": 2, "calls": 1, "cost": 0.01,
                     "normalised": 0}
    assert "2: Zwei" in ask.sent[0] and "1: Eins" not in ask.sent[0]
    assert {c.index: c.text for c in srt.read(secondary)} == {1: "раз", 2: "два", 3: "три"}


def test_a_rubric_change_re_translates_the_episode(episode, monkeypatch):
    primary, secondary = episode
    monkeypatch.setattr(translate.claude, "ask", answering(["1: раз\n2: два\n3: три"]))
    translate.episode(primary, secondary)

    monkeypatch.setattr(translate_prompt, "FINGERPRINT", "different")
    ask = answering(["1: один\n2: пара\n3: тройка"])
    monkeypatch.setattr(translate.claude, "ask", ask)
    stats = translate.episode(primary, secondary)

    assert stats["cached"] == 0 and stats["translated"] == 3
    assert {c.index: c.text for c in srt.read(secondary)}[1] == "один"


def test_redo_ignores_everything_already_translated(episode, monkeypatch):
    primary, secondary = episode
    monkeypatch.setattr(translate.claude, "ask", answering(["1: раз\n2: два\n3: три"]))
    translate.episode(primary, secondary)

    monkeypatch.setattr(translate.claude, "ask", answering(["1: один\n2: пара\n3: тройка"]))
    stats = translate.episode(primary, secondary, redo=True)

    assert stats["cached"] == 0
    assert {c.index: c.text for c in srt.read(secondary)}[3] == "тройка"


def test_an_empty_primary_is_refused_before_any_call(episode, monkeypatch):
    primary, secondary = episode
    primary.write_text("", encoding="utf-8")
    with pytest.raises(translate.TranslateError, match="no cues"):
        translate.episode(primary, secondary)  # conftest's stub would fire on a real call


# ── `gigaku translate`: which episodes ───────────────────────────────────────

@pytest.fixture
def library(tmp_path):
    """A library with one show: E01 has both tracks, E02 and E03 only a Primary."""
    show = tmp_path / "Dark"
    show.mkdir()
    for number in (1, 2, 3):
        srt.write(show / f"Dark - S02E0{number} - Primary.srt", [cue(1, "Eins")])
    srt.write(show / "Dark - S02E01 - Secondary.srt", [cue(1, "Раз")])
    return tmp_path


def names(pairs):
    return [primary.name for primary, _ in pairs]


def test_only_episodes_missing_a_secondary(library):
    assert names(translate.pending(library)) == [
        "Dark - S02E02 - Primary.srt", "Dark - S02E03 - Primary.srt"]


def test_redo_takes_the_finished_one_too(library):
    assert len(translate.pending(library, redo=True)) == 3


def test_filters_by_show_season_and_range(library):
    from lib.subs import episode_range

    assert names(translate.pending(library, name="dark")) == names(translate.pending(library))
    assert translate.pending(library, name="Blue Lock") == []
    assert translate.pending(library, season=1) == []
    assert names(translate.pending(library, season=2, episodes=episode_range.parse("3-"))) == [
        "Dark - S02E03 - Primary.srt"]


def test_a_missing_library_is_empty_not_a_crash(tmp_path):
    assert translate.pending(tmp_path / "nope") == []


# ── the dictionary pass ──────────────────────────────────────────────────────
#
# `parse_normalise` is the guard between a one-call form-fixer and the episode it rewrites, so
# what is pinned here is what it *refuses*. Each case below is a way the reply can be wrong
# while still looking like an answer — an absent reply is already handled by the caller.

def test_normalise_keeps_only_a_form_change():
    tokens = ["команда_коллеги", "пас_пути", "команда_игра"]
    reply = ("команда_коллеги -> команда_коллега\n"
             "пас_пути -> пас_путь\n"
             "команда_игра -> команда_игра\n")
    # An unchanged token is not a change, so it never reaches the substitution.
    assert translate_prompt.parse_normalise(reply, tokens) == {
        "команда_коллеги": "команда_коллега", "пас_пути": "пас_путь"}


def test_normalise_refuses_a_token_it_was_not_asked_about():
    # Otherwise a hallucinated pair could rewrite text nobody submitted for checking.
    assert translate_prompt.parse_normalise("выдумка_раз -> нечто", ["пас_пути"]) == {}


def test_normalise_refuses_a_reply_that_merges_or_splits_parts():
    # The pass asks for a *form*, never a re-gloss. A reply with a different number of parts is
    # answering a different question, and taking it would let a whole token quietly change
    # meaning — the one thing string substitution cannot be allowed to do.
    tokens = ["мяч_приёмки", "игра_поле"]
    reply = "мяч_приёмки -> мяч приёмка\nигра_поле -> игра_поле_большое\n"
    assert translate_prompt.parse_normalise(reply, tokens) == {}


def test_normalise_takes_the_first_answer_for_a_token():
    # A model that repeats itself must not overwrite the line it already gave.
    reply = "пас_пути -> пас_путь\nпас_пути -> пас_ПОВТОР\n"
    assert translate_prompt.parse_normalise(reply, ["пас_пути"]) == {"пас_пути": "пас_путь"}


# ── the name glossary ────────────────────────────────────────────────────────
#
# At group 200 an episode was two requests and the context lines carried the cast across.
# At 50 it is eight, so the spellings have to be handed forward explicitly.

def test_names_skips_a_sentence_initial_capital():
    lines = {1: "Привет, Бароу!", 2: "Что ты делаешь?", 3: "Тогда Исаги ушёл."}
    # «Что» and «Тогда» open their sentences and are grammar, not names.
    assert translate_prompt.names(lines) == ["Бароу", "Исаги"]


def test_render_puts_known_names_before_the_lines_as_an_instruction():
    view = [(cue(1, "Hallo"), True)]
    rendered = translate_prompt.render(view, ["Бароу", "Исаги"])
    assert rendered.index("Бароу") < rendered.index("Translate the 1 line")
    assert "do not invent a second spelling" in rendered


def test_render_says_nothing_about_names_when_none_are_known_yet():
    assert translate_prompt.render([(cue(1, "Hallo"), True)]).startswith("Translate the")


# ── the unjoin pass ──────────────────────────────────────────────────────────
#
# It can only ever take a mark off. That is the whole safety argument, so it is what is pinned.

def test_unjoin_takes_only_the_split_verdicts():
    tokens = ["рот_на_замок", "игра_поле"]
    assert translate_prompt.parse_unjoin(
        "рот_на_замок SPLIT\nигра_поле KEEP\n", tokens) == {"рот_на_замок"}


def test_unjoin_ignores_a_token_it_was_not_asked_about():
    assert translate_prompt.parse_unjoin("выдумка_раз SPLIT", ["игра_поле"]) == set()


def test_unjoin_treats_silence_as_keep():
    # A reply that names nothing leaves the episode exactly as glossed.
    assert translate_prompt.parse_unjoin("", ["игра_поле"]) == set()
