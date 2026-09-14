"""Kanji ranking, the Anki query, and the daily cumulative series the plots page draws."""
import re
from datetime import datetime, timedelta

import pytest

from lib.vocab import kanji, plots
from lib.config import UserError, settings
from lib.vocab.words import Stage, Word

TODAY = datetime.now()


def w(text, lang="ja", stage=Stage.KNOWN, date=None):
    return Word(text, lang, stage, date or TODAY)


def test_kanji_ranked_by_known_then_learning_words():
    found = kanji.collect([
        w("日本"), w("日曜日"), w("本"),
        w("犬", stage=Stage.LEARNING),
    ])
    ranked = {k.char: (len(k.known), len(k.learning)) for k in found}

    assert ranked["日"] == (2, 0)  # 日本 and 日曜日 — one word counts once, not once per 日
    assert ranked["本"] == (2, 0)  # 日本 and 本
    assert ranked["曜"] == (1, 0)
    assert ranked["犬"] == (0, 1)
    assert found[0].char == "日"  # best first: most known words, then most learning


def test_kana_and_latin_are_not_kanji():
    found = kanji.collect([w("ひらがな"), w("カタカナ"), w("ok")])
    assert found == []


def test_only_japanese_words_count():
    assert kanji.collect([w("漢字", lang="zh")]) == []


def test_anki_query_stops_at_the_minimum_counts(monkeypatch):
    monkeypatch.setattr(settings, "ANKI_MIN_COUNTS", "2,0")
    monkeypatch.setattr(settings, "ANKI_FILTERS", "deck:漢字 is:suspended")
    monkeypatch.setattr(settings, "ANKI_KANJI_FIELD", "kanji")

    # 日 is in two known words; 本, 曜 and 犬 are each in one, so the query stops after 日.
    query = kanji.anki_query(kanji.collect([w("日本"), w("日曜日"), w("犬")]))
    assert query == "deck:漢字 is:suspended (kanji:日)"


def test_anki_min_counts_are_an_and_not_a_tuple_comparison(monkeypatch):
    """`(known, learning) < (K, L)` is lexicographic: it would wave through a kanji with many
    known words and zero learning ones — exactly what the second number exists to exclude."""
    monkeypatch.setattr(settings, "ANKI_MIN_COUNTS", "2,2")
    monkeypatch.setattr(settings, "ANKI_FILTERS", "deck:漢字")
    monkeypatch.setattr(settings, "ANKI_KANJI_FIELD", "kanji")

    words = [
        w("日本"), w("日曜日"), w("日記"),  # 日: 3 known, 0 learning → fails the learning bar
        w("犬小屋", stage=Stage.LEARNING), w("犬派", stage=Stage.LEARNING),
        w("犬"), w("愛犬"),  # 犬: 2 known, 2 learning → the only one that clears both bars
    ]
    assert kanji.anki_query(kanji.collect(words)) == "deck:漢字 (kanji:犬)"


def test_anki_query_keeps_looking_past_a_kanji_that_misses(monkeypatch):
    """The list is ranked by known-count, so the first miss says nothing about the rest —
    a `break` there would silently truncate the query."""
    monkeypatch.setattr(settings, "ANKI_MIN_COUNTS", "1,1")
    monkeypatch.setattr(settings, "ANKI_FILTERS", "deck:漢字")
    monkeypatch.setattr(settings, "ANKI_KANJI_FIELD", "kanji")

    words = [
        w("日本"), w("日曜日"), w("日記"),  # 日 ranks first, 0 learning → misses
        w("犬"), w("犬小屋", stage=Stage.LEARNING),  # 犬 ranks lower but clears both
    ]
    assert "kanji:犬" in kanji.anki_query(kanji.collect(words))


def test_anki_query_says_so_when_nothing_qualifies(monkeypatch):
    monkeypatch.setattr(settings, "ANKI_MIN_COUNTS", "99,99")
    assert "ANKI_MIN_COUNTS" in kanji.anki_query(kanji.collect([w("日本")]))


def test_bad_min_counts_is_a_user_error(monkeypatch):
    monkeypatch.setattr(settings, "ANKI_MIN_COUNTS", "seven")
    with pytest.raises(UserError, match="two integers"):
        settings.anki_min_counts()


# ── plots ────────────────────────────────────────────────────────────────────


def test_series_are_daily_cumulative_counts():
    data = plots.build([
        w("犬", date=datetime(2026, 1, 1)),
        w("猫", date=datetime(2026, 1, 3)),
        w("鳥", stage=Stage.LEARNING, date=datetime(2026, 1, 3)),
    ])
    (ja,) = data["series"]

    assert data["start"] == "2026-01-01"
    assert data["days"] == (datetime.now().date() - datetime(2026, 1, 1).date()).days + 1
    assert ja["known"][:3] == [1, 1, 2]  # cumulative: never dips, holds on quiet days
    assert ja["learning"][:3] == [0, 0, 1]
    assert ja["known"][-1] == 2 and ja["learning"][-1] == 1  # curves run up to today


def test_a_language_keeps_its_colour_slot_whoever_else_is_present():
    solo = plots.build([w("Haus", lang="de")])["series"][0]["slot"]
    crowd = {s["code"]: s["slot"] for s in plots.build([
        w("Haus", lang="de"), w("犬"), w("chien", lang="fr")
    ])["series"]}

    assert solo == crowd["de"]  # filtering languages must not repaint the survivors
    assert len(set(crowd.values())) == 3


def test_unplanned_languages_get_a_free_slot():
    data = plots.build([w("hej", lang="sv"), w("Haus", lang="de")])
    slots = {s["code"]: s["slot"] for s in data["series"]}
    assert slots["de"] == plots.LANG_SLOTS["de"]
    assert slots["sv"] != slots["de"]


def test_an_unplanned_language_cannot_repaint_the_reserved_ones():
    """One stray word in a language nobody planned for must not cascade every other language
    onto a new colour — 'ar' sorts before 'de', and the naive fill handed it German's slot."""
    before = {s["code"]: s["slot"] for s in plots.build([w("Haus", lang="de"), w("犬")])["series"]}
    after = {s["code"]: s["slot"] for s in plots.build([
        w("Haus", lang="de"), w("犬"), w("كتاب", lang="ar")
    ])["series"]}

    assert after["de"] == before["de"] == plots.LANG_SLOTS["de"]
    assert after["ja"] == before["ja"] == plots.LANG_SLOTS["ja"]
    assert after["ar"] not in (after["de"], after["ja"])


def test_a_word_dated_in_the_future_does_not_crash_the_plot():
    """A device with a fast clock bakes a future timestamp into the cache for good; sizing the
    day axis to today alone would then index past the end of the list on every single run."""
    ahead = datetime.now() + timedelta(days=9)
    data = plots.build([w("犬", date=datetime(2026, 1, 1)), w("猫", date=ahead)])

    (ja,) = data["series"]
    assert data["days"] == (ahead.date() - datetime(2026, 1, 1).date()).days + 1
    assert ja["known"][-1] == 2  # the future word is counted, at the end of the axis


def test_plotting_nothing_is_a_user_error():
    with pytest.raises(UserError, match="import"):
        plots.build([])


def test_the_page_carries_the_same_kanji_table_the_tsv_prints():
    words = [w("日本"), w("日曜日"), w("犬", stage=Stage.LEARNING)]
    data = plots.build(words)

    assert [k["char"] for k in data["kanji"]] == [k.char for k in kanji.collect(words)]
    top = data["kanji"][0]
    assert top["char"] == "日"
    assert top["known"] == ["日曜日", "日本"]  # the words themselves ride along, for the filter
    assert top["learning"] == []


def test_no_japanese_means_no_kanji_tab():
    assert plots.build([w("Haus", lang="de")])["kanji"] == []


def test_rendered_page_is_self_contained(tmp_path):
    data = plots.build([w("犬", date=datetime(2026, 1, 1))])
    page = tmp_path / "plots.html"
    plots.render(data, str(page))
    html = page.read_text(encoding="utf-8")

    assert "__DATA__" not in html and "__TITLE__" not in html and "__BASE_CSS__" not in html
    # No server, no CDN, no fetch: the file works offline, forever, and can be mailed.
    # A <link> is allowed only as the data:-URI favicon — that's bytes, not network.
    assert 'src="http' not in html and 'href="http' not in html and "fetch(" not in html
    for tag in re.findall(r"<link[^>]*>", html):
        assert 'href="data:' in tag, f"non-inline <link>: {tag}"
    assert '"known":[1' in html.replace(" ", "")
    # The palette arrives via the payload — the page must not carry a second copy.
    assert '"palette"' in html and html.count("#3987e5") == 1
    # The tab is named after the app; PLOTS_TITLE is the heading *inside* the page.
    assert "<title>Gigaku</title>" in html
    assert f'"title":"{settings.PLOTS_TITLE}"' in html.replace(" ", "")
