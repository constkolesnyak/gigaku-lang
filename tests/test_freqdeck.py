"""The pure parts of the Frequency 🇩🇪 pipeline (freqdeck/): parsing, naming, the flag
rule and the add-on's field formats. Nothing here talks to Claude, OpenAI, Commons or Anki."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "freqdeck"))
import deck  # noqa: E402
import definitions  # noqa: E402


def test_parse_sentences_keeps_only_asked_ids_first_answer_wins():
    batch = [("Koffer", "чемодан"), ("regnen", "дождь идти")]
    reply = ("1/1: Ich hab den Koffer schon gepackt.\n1/2: Nimm den kleinen Koffer.\n"
             "1/3: Der Koffer ist zu schwer.\n1/3: (duplicate, ignored)\n"
             "2/1: Es regnet.\n3/1: invented id\n2/4: fourth candidate, ignored\nnoise line")
    got = deck.parse_sentences(reply, batch)
    assert got[("Koffer", 3)] == "Der Koffer ist zu schwer."
    assert got[("regnen", 1)] == "Es regnet."
    assert set(k[0] for k in got) == {"Koffer", "regnen"} and ("regnen", 4) not in got


def test_media_names_are_lowercase_ascii_and_stable():
    # Anki lowercases stored names; a capital in the tag then points at a file that is not
    # there after a sync (measured 2026-08-26).
    assert deck.media_name(49, "Träne") == "freq_de_049_traene.mp3"
    assert deck.media_name(1, "Macht") == "freq_de_001_macht.mp3"
    assert deck.ascii_slug("Bürgermeister") == "buergermeister"
    assert deck.ascii_slug("U-Bahn") == "ubahn"
    assert deck.ascii_slug("ß") == "ss"


def test_flags_are_the_intersection_of_both_witnesses():
    lemmatize = lambda w: {"Schirm": "Schirm", "geregnet": "regnen", "hab": "haben",
                           "Kleid": "Kleid"}.get(w, w)
    unknown = {"schirm", "kleid", "hab", "regnen"}          # de-words.tsv lists these
    known = {"haben", "regnen"}                             # Migaku knows these
    fl = deck.flags("Nimm den Schirm mit, es hat geregnet, ich hab kein Kleid.", "regnen",
                    unknown, known, lemmatize)
    assert fl == ["Schirm", "Kleid"]        # hab → haben is known; the target is excluded


def test_flags_ignore_the_targets_stranded_separable_prefix():
    lemmatize = lambda w: w
    assert deck.flags("Mach das Licht an.", "anmachen", {"an"}, set(), lemmatize) == []


def test_definition_field_formats_match_the_addons():
    block = definitions.definition_block("A suitcase.")
    assert block == '<!-- def-type="bilingual" -->\nA suitcase.\n<!-- def-end -->'
    assert definitions.bilingual_text(block) == "A suitcase."
    audio = definitions.audio_block("A suitcase.", "mvj-bilingual-definition-1.mp3")
    assert audio.startswith('<!-- def-type="bilingual" TTS-SOURCE: A suitcase. -->\n[audio:mvj-bilingual-definition-1.mp3]')
    assert "monolingual" not in block + audio


def test_words_reads_the_active_lists_head(tmp_path, monkeypatch):
    p = tmp_path / "list.tsv"
    p.write_text("Macht\tвласть\nwachsen\tрасти\nlinks\n", encoding="utf-8")
    monkeypatch.setitem(deck.LISTS, "probe", {"file": p.name, "prefix": "probe_de", "cache": "probe-"})
    monkeypatch.setattr(deck, "ROOT", str(tmp_path))
    deck.use_list("probe")
    try:
        assert deck.words(2) == [("Macht", "власть"), ("wachsen", "расти")]
        assert deck.words(10)[-1] == ("links", "")
        # each list keeps its own caches and file prefix, or the second thousand overwrites the first
        assert deck.CHOSEN.endswith("probe-chosen.tsv")
        assert deck.media_name(1, "Macht") == "probe_de_001_macht.mp3"
        assert deck.locate("wachsen") == ("probe", 2)
    finally:
        deck.use_list("words")


def test_lists_do_not_share_a_cache_or_a_media_prefix():
    names = sorted(deck.LISTS)
    assert len(names) > 1
    caches = {deck.LISTS[n]["cache"] for n in names}
    prefixes = {deck.LISTS[n]["prefix"] for n in names}
    assert len(caches) == len(names) and len(prefixes) == len(names)


def test_skip_holds_the_lists_pseudo_words():
    assert {"unter-", "dar"} <= deck.SKIP


# ── the deck's order (freqdeck/order.py) ────────────────────────────────────────────
import order  # noqa: E402


LIVING = {"Macht": 94.9, "wachsen": 79.4, "teilweise": 5.7, "Sarg": 15.4}
YT_PM = {"Macht": 5.4, "teilweise": 177.2, "streamen": 52.6, "Einatmen": 49.7}
CHANNELS = {"Macht": 13, "teilweise": 216, "streamen": 96, "Einatmen": 8}
WORDS = ["Macht", "wachsen", "teilweise", "Sarg", "streamen", "Einatmen"]


def test_absence_in_one_corpus_does_not_sink_a_word():
    """The score is expected exposure, so it SUMS the corpora. A product (tried first)
    made an absent corpus a zero: `wachsen`, second by general frequency but unattested
    in the user's subtitles, fell to #1546, and `streamen` — absent from the general list —
    to #1592."""
    ranked, _ = order.order_for(WORDS, LIVING, YT_PM, CHANNELS)
    assert ranked.index("wachsen") < ranked.index("Sarg")        # carried by the general list
    assert ranked.index("streamen") < ranked.index("Sarg")       # carried by the user's subtitles alone
    assert ranked[0] == "teilweise"                              # high in the user's subs, well spread


def test_a_rate_off_a_handful_of_channels_is_damped():
    """`Einatmen` reads 49.7/M off 8 channels — yoga videos repeating one instruction."""
    ranked, score = order.order_for(WORDS, LIVING, YT_PM, CHANNELS)
    assert ranked.index("streamen") < ranked.index("Einatmen")
    wide = dict(CHANNELS, Einatmen=200)                          # same rate, spread out
    assert order.scores(WORDS, LIVING, YT_PM, wide)["Einatmen"] > score["Einatmen"]


def test_order_is_reproducible_and_total():
    ranked, score = order.order_for(WORDS, LIVING, YT_PM, CHANNELS)
    assert sorted(ranked) == sorted(WORDS) and len(set(ranked)) == len(WORDS)
    assert order.order_for(list(reversed(WORDS)), LIVING, YT_PM, CHANNELS)[0] == ranked
    assert all(w in score for w in WORDS)


def test_a_word_in_neither_corpus_goes_last_but_is_not_dropped():
    ranked, score = order.order_for(WORDS + ["Blödsinn"], LIVING, YT_PM, CHANNELS)
    assert ranked[-1] == "Blödsinn" and score["Blödsinn"] == 0


# ── loudness (freqdeck/loudness.py) ─────────────────────────────────────────────────
import loudness  # noqa: E402


def test_the_gain_brings_a_file_to_the_target():
    """Measured 2026-08-28: Commons word recordings ran −36.3…−9.6 LUFS (σ 2.88) while
    the TTS sat at −23.4 (σ 0.75). Each word gets one fixed gain onto that level."""
    assert loudness.gain_for(-16.0, -6.0, -23.4) == pytest.approx(-7.4)
    assert loudness.gain_for(-36.3, -20.0, -23.4) == pytest.approx(12.9)


def test_the_ceiling_only_ever_holds_a_file_back_from_being_raised():
    """A quiet file whose peak already sits at −0.1 dBFS cannot be brought up to the
    target — it stays quieter instead of clipping. Turning a file DOWN is never held
    back by the ceiling: a loud Commons word peaking at +0.3 still gets its full −4 dB
    (and its peak lands at −3.7 on the way)."""
    assert loudness.gain_for(-24.0, -0.1, -20.0) == pytest.approx(-0.9)
    assert loudness.gain_for(-16.0, 0.3, -20.0) == pytest.approx(-4.0)


def test_silence_is_left_alone():
    """ebur128 reports −70 LUFS for near-silence; a huge gain would only raise its noise."""
    assert loudness.gain_for(None, -3.0, -20.0) == 0.0
    assert loudness.gain_for(-70.0, -3.0, -20.0) == 0.0
    assert loudness.gain_for(-91.0, None, -20.0) == 0.0


def test_a_missing_peak_still_normalises():
    assert loudness.gain_for(-26.0, None, -20.0) == pytest.approx(6.0)


def test_the_tts_is_the_reference_and_is_never_adjusted():
    """The user's call: match the words to the rest, don't move the rest. The TTS is two
    thirds of the files and already even (σ 0.75 dB), so it is measured, not re-encoded."""
    assert loudness.ADJUST == ("Word Audio",)
    assert set(loudness.REFERENCE) == {"Sentence Audio", "Definition Audio"}
    assert not set(loudness.ADJUST) & set(loudness.REFERENCE)
