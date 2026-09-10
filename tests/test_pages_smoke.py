"""Headless-Chrome smoke tests — the first coverage that *executes* the pages' JavaScript.

Everything else asserts about the rendered string; a typo inside <script> passes all of it
and ships a blank page. Chrome is already a project requirement (CHROME_APP renders the
report cards), so each page is rendered with fixture data, loaded headless, and the DOM
dumped after script execution: a chart that drew leaves <path> elements, a table that
built leaves rows, and a crashed script leaves neither. --dump-dom normally exits on its
own, but Chrome is never trusted to (the report renderer measured it not exiting), so
every run carries a timeout and the error path kills.
"""
import os
import re
import subprocess
from datetime import datetime

import pytest

from lib.config import settings
from lib.netflix import store, titles
from lib.vocab import plots, report
from lib.vocab.words import Stage, Word

CHROME = settings.CHROME_APP
pytestmark = pytest.mark.skipif(not os.path.exists(CHROME), reason="Chrome not installed")


def dump_dom(html_path, tmp_path):
    command = [
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        f"--user-data-dir={tmp_path}/chrome-profile",
        "--virtual-time-budget=3000",  # let the page's own scripts finish before the dump
        "--dump-dom", f"file://{html_path}",
    ]
    # Chrome writes the dump and then — measured here exactly as lib/vocab/report.py
    # measured it with PNGs — does not exit. Same answer: the output is the signal, the
    # process is killed. run(timeout=) kills and hands over the stdout captured so far;
    # the closing </html> is the receipt that the dump is whole.
    try:
        result = subprocess.run(command, capture_output=True, timeout=25)
        out = result.stdout
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
    dom = out.decode("utf-8", "replace")
    assert "</html>" in dom, "Chrome produced no complete DOM dump"
    return dom


WORDS = [
    Word("犬", "ja", Stage.KNOWN, datetime(2026, 1, 1)),
    Word("猫", "ja", Stage.LEARNING, datetime(2026, 2, 1)),
    Word("Haus", "de", Stage.KNOWN, datetime(2026, 3, 1)),
]


def test_plots_page_draws_its_chart_and_tiles(tmp_path):
    out = tmp_path / "plots.html"
    plots.render(plots.build(WORDS), str(out))

    dom = dump_dom(out, tmp_path)

    assert "__DATA__" not in dom and "__BASE_CSS__" not in dom
    assert dom.count("<path") >= 2  # the chart drew its lines, i.e. the script ran to the end
    assert "Japanese" in dom and "German" in dom  # legend + tiles built
    assert "Theme: Auto" in dom  # the toggle initialised


def test_titles_page_builds_its_table(tmp_path):
    dump = {
        "name": "Smoke", "url": "https://example.invalid", "lang": "de",
        "fetched_at": 1_700_000_000, "complete": True,
        "records": [store.record(0, {
            "itemSummary": {"videoId": 1, "title": "Dark", "type": "show",
                            "releaseYear": 2017},
            "queue": {"inQueue": False}, "inRemindMeList": False,
        })],
    }
    dump["records"][0].update({"imdb_rating": 8.7, "imdb_votes": 400_000, "weighted": 8.6,
                               "imdb_genres": ["Drama"], "match_confidence": "exact"})
    out = titles.render(titles.build(dump, titles.stats_for(dump["records"])),
                        str(tmp_path / "titles.html"))

    dom = dump_dom(out, tmp_path)

    assert "__DATA__" not in dom
    assert "Dark" in dom and "8.7" in dom  # the row actually rendered


def test_report_card_draws_every_language_in_one_coordinate_system(tmp_path):
    data = report.build(WORDS, today=datetime(2026, 3, 2).date())
    out = tmp_path / "card.html"
    report.render_card(data, str(out))

    dom = dump_dom(out, tmp_path)
    # The script's own source is in the DOM too, and it contains the markup it builds —
    # counting what was rendered means counting outside it.
    rendered = re.sub(r"(?s)<script>.*?</script>", "", dom)

    assert "__DATA__" not in dom
    assert rendered.count("<svg") == 1  # one chart, not one per language
    assert rendered.count("<polyline") == len(data["languages"]) > 1  # …with a curve each
    assert "German" in rendered and "Japanese" in rendered  # the header is the legend
