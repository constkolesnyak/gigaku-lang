"""`gigaku plots` — render the vocabulary history as a self-contained HTML page and open it.

What this module does is turn the word store into *daily cumulative counts per language and
stage*, hand them to lib/vocab/plots_page.html, and write one file. The drawing happens there, in
SVG, by hand.

polyglotka did this with Dash + Plotly + waitress: it booted a local web server, opened a
browser at it, and then called os._exit(0) from a background thread to kill the server once
the tab had loaded. That's five dependencies and a race to display a chart. A file on disk
has neither, opens instantly, survives a reboot, and can be sent to someone.

The counts themselves are honest about what the data can say: a word carries only the date
its stage was *last* modified, so "known words on 3 March" means "words whose current stage
is KNOWN and which were last touched on or before 3 March". Both sources are like this;
there is no revision history to be had.
"""
import os
import webbrowser
from collections import defaultdict
from datetime import date

from lib import pages
from lib.config import UserError, note, settings
from lib.vocab.words import UNKNOWN_DATE, Stage

PAGE = os.path.join(os.path.dirname(__file__), "plots_page.html")

# A language keeps its colour slot no matter which other languages are in the chart — the
# reader who learned "German is blue" is never contradicted. (dataviz: colour follows the
# entity, never its rank.) Slots index the categorical palette in plots_page.html.
LANG_SLOTS = {"de": 0, "ja": 1, "fr": 2, "ko": 3, "es": 4, "it": 5, "zh": 6, "en": 7}

LANG_NAMES = {
    "ar": "Arabic", "de": "German", "en": "English", "es": "Spanish", "fr": "French",
    "hi": "Hindi", "it": "Italian", "ja": "Japanese", "ko": "Korean", "nl": "Dutch",
    "pl": "Polish", "pt": "Portuguese", "ru": "Russian", "sv": "Swedish", "tr": "Turkish",
    "uk": "Ukrainian", "zh": "Chinese",
}


def _slots(languages):
    """Map every language to its colour slot, honouring the reservations in LANG_SLOTS.

    Reserved languages are placed *first*, before any leftovers are handed out. Filling slots
    greedily in alphabetical order instead would let an unplanned language (say Arabic) take
    slot 0 and cascade German onto Japanese's colour and Japanese onto French's — the whole
    chart repainted by one stray word, which is the exact thing the reservations exist to
    prevent. A language nobody planned for gets whatever is left over, and only that.
    """
    slots = {lang: LANG_SLOTS[lang] for lang in languages if lang in LANG_SLOTS}
    spare = [s for s in range(8) if s not in set(slots.values())]
    for lang in languages:
        if lang not in slots:
            slots[lang] = spare.pop(0) if spare else len(slots) % 8
    return slots


def _kanji(words):
    """The kanji table the page's second tab draws — same ranking `gigaku kanji` prints.

    Reuses lib/vocab/kanji.collect() rather than re-deriving it, so the page and the TSV can never
    disagree about what you know. Empty for anyone not studying Japanese: no tab, then.
    """
    from lib.vocab import kanji

    return [
        {"char": k.char, "known": sorted(k.known), "learning": sorted(k.learning)}
        for k in kanji.collect(words)
    ]


def build(words):
    """Daily cumulative counts, ready for the page.

    One shared day axis (first word → today, so the curves run up to the present rather than
    stopping at whenever you last studied), and per language a cumulative `known` and
    `learning` array over it."""
    if not words:
        raise UserError("No words to plot. Run `gigaku import` first.")

    # The axis starts at the earliest *recorded* date. A word stamped UNKNOWN_DATE is known
    # but undated (AnkiMorphs has no such timestamp — see lib/vocab/ankimorphs.py), and
    # honouring it here would stretch the axis back two thousand years and allocate a
    # 740,000-day array per language. Those words fold into day 0 below instead, which is
    # what "known since before the record starts" looks like on a cumulative curve.
    dated = [w.date.date() for w in words if w.date != UNKNOWN_DATE]
    start = min(dated, default=date.today())
    # The axis has to cover the last *word*, not just today: Migaku's `mod` and LR's
    # `timeModified_ms` come from whichever machine's clock wrote them, so a device running
    # ahead bakes a future date into the cache permanently — and indexing a today-sized list
    # with it would crash `plots` on every run, with no way to edit the word out from the CLI.
    end = max(max(dated, default=date.today()), date.today())
    days = (end - start).days + 1

    # Daily deltas first, then one pass to accumulate — O(words + days·langs), where
    # polyglotka's y-axis was an O(words × points) scan per trace.
    deltas = defaultdict(lambda: [0] * days)
    for word in words:
        # An undated word (and, for the same reason, anything older than the axis) lands on
        # day 0 rather than off the left edge: it is known, it just predates the record.
        index = max(0, (word.date.date() - start).days)
        deltas[(word.language, word.stage)][index] += 1

    languages = sorted({w.language for w in words})
    slots, series = _slots(languages), []
    for lang in languages:
        slot = slots[lang]

        counts = {}
        for stage in (Stage.KNOWN, Stage.LEARNING):
            running, cumulative = 0, []
            for day in deltas[(lang, stage)]:
                running += day
                cumulative.append(running)
            counts[stage.lower()] = cumulative

        series.append({
            "code": lang,
            "name": LANG_NAMES.get(lang, lang.upper()),
            "slot": slot,
            "known": counts["known"],
            "learning": counts["learning"],
        })

    return {
        "title": settings.PLOTS_TITLE,
        "start": start.isoformat(),
        "days": days,
        "total": len(words),
        "kanji": _kanji(words),
        "hidden": sorted(settings.hidden_langs() & set(languages)),
        "series": series,
        # The page draws with this rather than carrying its own copy — lib/pages.py is the
        # one palette the report card indexes too, so "German is blue" holds everywhere.
        "palette": pages.PALETTE,
    }


def render(data, out_path, nav=""):
    # The <title> is fixed ("Gigaku" — it names the tab); PLOTS_TITLE rides in the payload
    # and becomes the heading inside the page. `nav` is the published site's cross-link
    # (publish passes it); a locally opened plots.html has nowhere for it to point.
    pages.render(PAGE, out_path, {
        "__DATA__": pages.json_payload(data),
        "__BASE_CSS__": pages.BASE_CSS,
        "__NAV__": nav,
    })
    return out_path


def main(open_browser=True):
    from lib.vocab.words import import_words

    data = build(import_words())
    path = render(data, os.path.expanduser(settings.PLOTS_OUT))
    note(f'Plotted {data["total"]:,} words over {data["days"]:,} days → "{path}"')
    if open_browser:
        webbrowser.open(f"file://{path}")
