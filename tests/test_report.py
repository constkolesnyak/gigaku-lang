"""What the weekly Telegram report says — the arithmetic, not the pixels.

Everything the card draws and the message quotes comes out of `build()`, so this is where
both are pinned. The drawing itself (report_card.html) is geometry over these numbers and
headless Chrome's job; what has to be right here is *which* week a word lands in, that a
quiet week still reports, and that the picture and the text can't tell two different
stories about it.
"""
from datetime import date, datetime, timedelta

import pytest

from lib.config import UserError, settings
from lib.vocab import report
from lib.vocab.words import Stage, Word

TODAY = date(2026, 7, 31)


def w(text, lang="de", stage=Stage.KNOWN, day=None, month=7, year=2026):
    return Word(text, lang, stage, datetime(year, month, day or 31))


def build(words, **kw):
    kw.setdefault("today", TODAY)
    kw.setdefault("weeks", 4)
    return report.build(words, **kw)


def only(data):
    assert len(data["languages"]) == 1
    return data["languages"][0]


# ── the week ─────────────────────────────────────────────────────────────────


def test_the_week_is_the_last_complete_monday_to_sunday():
    # TODAY is Friday 31 Jul 2026; the week that has finished is Mon 20 – Sun 26.
    assert report.week_bounds(TODAY) == (date(2026, 7, 20), date(2026, 7, 26))
    assert report.week_bounds(TODAY, 1) == (date(2026, 7, 13), date(2026, 7, 19))


@pytest.mark.parametrize("fired", [date(2026, 8, 3), date(2026, 8, 5), date(2026, 8, 9)])
def test_a_late_run_reports_the_same_week_a_punctual_one_would(fired):
    # Monday 3 Aug is the schedule; Wednesday 5 Aug is a Mac that was asleep; Sunday 9 Aug
    # is a report host that came back late. All three must report Mon 27 Jul – Sun 2 Aug, or a
    # slipped run silently reports a window nothing else ever will.
    assert report.week_bounds(fired) == (date(2026, 7, 27), date(2026, 8, 2))


def test_the_week_never_includes_today():
    # Today is still being lived; a partial day counted as a week's worth reads as a slump.
    for day in range(1, 15):
        start, end = report.week_bounds(date(2026, 8, day))
        assert end < date(2026, 8, day)
        assert start.weekday() == 0 and end.weekday() == 6


def test_both_ends_of_the_week_count():
    # The reported week is Mon 20 – Sun 26; Monday and Sunday are in, the 27th is not.
    data = build([w("mon", day=20), w("sun", day=26), w("after", day=27), w("prev", day=15)])
    assert only(data)["week"] == 2
    assert only(data)["prev"] == 1


def test_a_quiet_week_reports_zero_and_says_how_long_ago():
    data = build([w("alt", month=6, day=10)])
    language = only(data)
    assert language["week"] == 0
    assert language["last_ago"] == "7 weeks ago"
    assert "+0" in report.message(data)


def test_the_bars_run_oldest_to_newest_and_end_on_the_reported_week():
    # Weeks 6–12, 13–19, 20–26 Jul; the last bucket is the week the report is about.
    data = build([w("a", day=8), w("b", day=22), w("c", day=23)], weeks=3)
    assert [b["count"] for b in only(data)["weeks"]] == [1, 0, 2]


def test_a_word_counts_in_the_week_its_stage_last_moved():
    # The only date a word carries is when its stage was last touched, so a word learnt in
    # March and re-touched today is this week's news. Nothing else is knowable — see plots.py.
    data = build([w("Haus", day=22)])
    assert only(data)["week"] == 1


# ── what is counted ──────────────────────────────────────────────────────────


def test_learning_words_are_not_new_known_words():
    data = build([w("a", day=22), w("b", stage=Stage.LEARNING, day=22)])
    language = only(data)
    assert (language["total"], language["week"], language["learning"]) == (1, 1, 1)


def test_languages_default_to_the_ones_plots_does_not_hide():
    settings.override(plots_hidden_langs="en,fr", report_langs="", validate=False)
    data = build([w("a"), w("b", lang="ja"), w("c", lang="fr"), w("d", lang="en")])
    assert [l["code"] for l in data["languages"]] == ["de", "ja"]


def test_report_langs_overrides_the_default():
    settings.override(plots_hidden_langs="en,fr", report_langs="fr", validate=False)
    data = build([w("a"), w("c", lang="fr")])
    assert [l["code"] for l in data["languages"]] == ["fr"]
    settings.override(report_langs="", validate=False)


def test_a_language_keeps_the_colour_the_plots_page_taught_you():
    # German is slot 0 and Japanese slot 1 in plots.LANG_SLOTS; the card must not repaint
    # them because a filter left one out.
    both = build([w("a"), w("b", lang="ja")])["languages"]
    alone = build([w("b", lang="ja")], langs=("ja",))["languages"]
    assert both[1]["color"] == alone[0]["color"] == report.PALETTE[1]
    assert both[0]["color"] == report.PALETTE[0]


def test_an_empty_cache_is_a_user_error_not_a_crash():
    with pytest.raises(UserError):
        build([])


# ── the shapes the card draws ────────────────────────────────────────────────


def test_the_curve_is_cumulative_and_reaches_the_total():
    data = build([w("a", month=7, day=29), w("b", month=7, day=31)])
    values = data["chart"]["series"][0]["values"]
    assert values[-1] == only(data)["total"] == 2
    assert values == sorted(values)


def test_the_curve_runs_to_today_even_when_the_last_word_is_old():
    # A language that has gone quiet is drawn flat all the way to the right edge, not cut
    # off at its last word — a curve that stops early reads as missing data.
    values = build([w("a", month=6, day=1)])["chart"]["series"][0]["values"]
    assert len(values) == report.CHART_DAYS
    assert values == [1] * report.CHART_DAYS


def test_every_language_is_drawn_in_one_shared_frame():
    # Curves on separate axes are separate charts wearing one frame. The frame is real: one
    # origin (the earliest word anybody has), one ceiling, one row of values per language —
    # so a language that started later runs along zero instead of being rescaled.
    data = build([w("early", month=7, day=6), w("late", lang="ja", month=7, day=30)])
    chart = data["chart"]
    assert chart["start"] == "2026-07-06"  # the earliest word anybody has, inside the window
    assert [s["code"] for s in chart["series"]] == ["de", "ja"]
    assert len({len(s["values"]) for s in chart["series"]}) == 1  # one x for both
    japanese = next(s for s in chart["series"] if s["code"] == "ja")
    assert japanese["values"][0] == 0 and japanese["values"][-1] == 1
    assert chart["ceiling"] >= max(s["values"][-1] for s in chart["series"])


@pytest.mark.parametrize("top,ceiling", [(6785, 7500), (2343, 3000), (9, 10), (0, 1)])
def test_the_axis_rounds_up_close_rather_than_to_the_next_power(top, ceiling):
    # A ceiling reached in two steps has to round 6,785 up to 10,000, and a curve drawn
    # into the bottom two thirds of its plot looks like less progress than it is.
    assert report._axis(top)[0] == ceiling


def test_the_axis_labels_start_at_zero_and_end_at_the_ceiling():
    ceiling, ticks = report._axis(6785)
    assert ticks[0] == 0 and ticks[-1] == ceiling


def test_the_x_axis_labels_both_ends_of_the_window():
    ticks = report._day_ticks(date(2026, 7, 3), 30)
    assert ticks[0] == {"i": 0, "label": "3 Jul"}
    assert ticks[-1] == {"i": 29, "label": "1 Aug"}  # …and the right-hand end is today
    assert [t["i"] for t in ticks] == sorted(t["i"] for t in ticks)


def test_the_chart_window_is_a_rolling_month_carrying_its_baseline():
    # The window is the last CHART_DAYS, but a cumulative curve inside it must still be the
    # running total — everything learnt before the window is the height it starts from, or
    # the chart would claim the count went back to zero a month ago.
    old = [w(f"old{i}", month=3, day=1) for i in range(40)]
    data = build(old + [w("new", day=30)])
    chart = data["chart"]
    values = chart["series"][0]["values"]
    assert len(values) == report.CHART_DAYS
    assert chart["start"] == (TODAY - timedelta(days=report.CHART_DAYS - 1)).isoformat()
    assert values[0] == 40 and values[-1] == 41 == only(data)["total"]


def test_the_shaded_band_is_the_reported_week_not_the_last_seven_days():
    # The band marks a calendar week that ended before today, so it must stop short of the
    # right edge — by more days the later a catch-up run fires.
    data = build([w("old", month=6, day=1), w("a", day=22)])
    chart, start, end = data["chart"], date(2026, 7, 20), date(2026, 7, 26)
    origin = date.fromisoformat(chart["start"])
    assert chart["week_from"] == (start - origin).days
    assert chart["week_to"] == (end - origin).days
    assert chart["week_to"] < len(chart["series"][0]["values"]) - 1


def test_a_history_shorter_than_the_window_is_not_padded_with_a_flat_run_in():
    data = build([w("a", day=29)])
    assert data["chart"]["start"] == "2026-07-29"
    assert len(data["chart"]["series"][0]["values"]) == 3  # 29, 30, 31 July


# ── the message ──────────────────────────────────────────────────────────────


def test_the_message_quotes_build_rather_than_recomputing_it():
    data = build([w("a", day=22), w("b", day=15), w("c", lang="ja", day=22)])
    text = report.message(data)
    for language in data["languages"]:
        assert f"{language['total']:,} known" in text
    assert "+1</b> last week" in text


def test_the_message_names_every_language_it_carded():
    data = build([w("a"), w("b", lang="ja")])
    text = report.message(data)
    assert "German" in text and "Japanese" in text
    assert text.index("German") < text.index("Japanese")  # the album's order


def test_the_message_opens_with_the_week_and_nothing_else():
    text = report.message(build([w("a")]))
    assert text.startswith("<b>📚 ")
    assert text.split("\n\n")[0].endswith(report.build([w("a")], today=TODAY)["span"])


def test_one_fact_per_bullet():
    # `2,350 known · +0 this week` was two facts wearing one bullet.
    data = build([w("a", day=22), w("b", day=23)])
    rows = [r for r in report.message(data).splitlines() if r.startswith("▸")]
    assert rows == ["▸ 2 known", "▸ <b>+2</b> last week", "▸ last word 8 days ago"]


def test_every_language_says_when_its_last_word_was_not_just_the_quiet_ones():
    # A busy language and a silent one. `last word` is what tells a +0 from a dead
    # pipeline, and a reader can only read it that way if it is always there.
    data = build([w("a", day=31), w("old", lang="ja", month=6, day=10)])
    assert report.message(data).count("last word ") == 2


def test_the_message_carries_this_week_and_nothing_else():
    # Removed one at a time by the user over 2026-08-01: the week before, the four-week
    # average, and every kanji number — asked for twice, so this pins the absence.
    words = [w("a", day=22), w("b", day=15), w("犬", lang="ja", day=22)]
    text = report.message(build(words))
    assert "week before" not in text and "over 4 weeks" not in text
    assert "kanji" not in text


def test_the_card_payload_replaces_the_sentinel(tmp_path):
    data = build([w("a"), w("b", lang="ja")])
    path = report.render_card(data, str(tmp_path / "card.html"))
    page = open(path, encoding="utf-8").read()
    # One page, every language: the whole week is a single screenshot and a single photo.
    assert "__DATA__" not in page
    assert '"code":"de"' in page and '"code":"ja"' in page
    # The payload rides inside a <script>; an unescaped "</" in it would close the tag early.
    assert "</script>" not in page.split("<script>")[1].split("</script>")[0]


def test_the_message_fits_a_caption_so_it_can_ride_on_the_photo():
    # One notification, not two — which only works while the text fits Telegram's cap.
    data = build([w("a"), w("b", lang="ja"), w("c", lang="fr"), w("d", lang="en")],
                 langs=("de", "ja", "fr", "en"))
    assert report._tg_len(report.message(data)) <= report.CAPTION_LIMIT


def _record(monkeypatch):
    """Capture (method, fields) instead of calling Telegram."""
    calls = []

    def fake(token, method, fields, files=None):
        calls.append((method, fields))
        return {"ok": True}

    monkeypatch.setattr(report, "tg_call", fake)
    return calls


def test_the_photo_carries_the_caption_so_the_week_is_one_notification(monkeypatch, tmp_path):
    png = _fake_png(tmp_path, "card.png")
    calls = _record(monkeypatch)
    report.send("t", "c", "<b>the week</b>", [png])
    assert [method for method, _ in calls] == ["sendPhoto"]
    assert calls[0][1]["caption"] == "<b>the week</b>"


def test_a_caption_too_long_still_gets_out_as_a_message_of_its_own(monkeypatch, tmp_path):
    # The cap is Telegram's, not ours: an oversized caption is a refused send, so the old
    # photo-then-message shape stays as the fallback rather than being deleted.
    png = _fake_png(tmp_path, "card.png")
    calls = _record(monkeypatch)
    report.send("t", "c", "x" * (report.CAPTION_LIMIT + 1), [png])
    assert [method for method, _ in calls] == ["sendPhoto", "sendMessage"]
    assert "caption" not in calls[0][1]


def test_the_screenshot_is_only_believed_once_the_png_says_it_is_finished(tmp_path):
    half = tmp_path / "half.png"
    half.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    assert not report._complete(str(half))
    done = tmp_path / "done.png"
    done.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + b"IEND\xae\x42\x60\x82")
    assert report._complete(str(done))
    assert not report._complete(str(tmp_path / "missing.png"))


# ── the site page ────────────────────────────────────────────────────────────


def _fake_png(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20 + b"IEND\xae\x42\x60\x82")
    return str(path)


def test_the_week_becomes_a_page_and_the_index_is_the_latest(tmp_path):
    data = build([w("a")])
    site = tmp_path / "site"
    report.site(data, [_fake_png(tmp_path, "card.png")], "<b>+1</b> this week",
                directory=str(site), today=TODAY)

    week = (site / "2026-07-31.html").read_text(encoding="utf-8")
    index = (site / "index.html").read_text(encoding="utf-8")
    assert "__" not in week.replace("__BASE", "KEPT")  # no sentinel survived
    assert "data:image/png;base64," in week  # the card rides inside the page
    assert "<b>+1</b> this week" in week
    assert week == index  # the index *is* the latest week


def test_the_archive_accumulates_and_lists_newest_first(tmp_path):
    site = tmp_path / "site"
    png = [_fake_png(tmp_path, "card.png")]
    report.site(build([w("a")]), png, "week one", directory=str(site), today=date(2026, 7, 24))
    report.site(build([w("a")]), png, "week two", directory=str(site), today=TODAY)

    index = (site / "index.html").read_text(encoding="utf-8")
    assert "week two" in index
    assert index.index("2026-07-31") < index.index("2026-07-24")  # newest first
    assert (site / "2026-07-24.html").exists()  # last week's page still stands
