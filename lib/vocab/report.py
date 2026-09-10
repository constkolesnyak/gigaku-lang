"""`gigaku report` — the week just gone, as a Telegram post: one chart card per language.

`gigaku plots` answers "how is it going" only if you remember to go and look, and
`gigaku publish` puts the same page behind a URL you also have to remember. This is the
other direction: once a week the numbers come to *you*, in the one place a phone already
interrupts for. Same data, same colours, no page to open.

Three things this has to get right, and each of them is why a step here looks the way it
does:

**The week is what the data can actually say.** A word carries only the date its stage was
*last* modified — both sources are like that, there is no revision history (see plots.py) —
so "+258 this week" means "258 words whose current stage is KNOWN and which last moved in
the last seven days". A word learnt in March and re-touched today counts today. That is the
only week there is to report, and the card says `new` rather than `learnt` because of it.

**A quiet week is a result, not a failure to gather data.** The report never skips, never
rounds up and never hides a zero behind a friendlier number: it prints `+0` and says how
long ago the last word actually was. A weekly notification that only arrives when the news
is good is a notification you stop believing.

**Charts have to survive being a picture.** Telegram renders a PNG, so nothing here can lean
on a tooltip: every number a reader needs is either a stat tile or a direct label on the
mark. The card is drawn by report_card.html — a self-contained page, hand-written SVG,
exactly like plots_page.html — and turned into a PNG by headless Chrome, which the project
already depends on everywhere else. Nothing new was installed to draw a line.

    gigaku report                # dry run: write the PNGs, print the message
    gigaku report --send         # post it to Telegram
    gigaku report --weeks 20     # more bars
    gigaku report --from-backup  # read vocab-backup's words.json instead of the live cache
"""
import base64
import datetime as dt
import json
import math
import os
import re
import subprocess
import time

from lib import pages
from lib.config import UserError, note, settings
from lib.telegram import credentials, tg_call
from lib.vocab.plots import LANG_NAMES, _slots
from lib.vocab.words import Stage

CARD = os.path.join(os.path.dirname(__file__), "report_card.html")
SITE_PAGE = os.path.join(os.path.dirname(__file__), "report_page.html")

# The dark row of the one palette (lib/pages.py) — the same array the page draws with,
# indexed by the same slot. A language must keep the colour the page taught you: German is
# blue in the browser, so German is blue on the phone. A card is a dark PNG, so dark row.
PALETTE = tuple(pages.PALETTE["dark"])

NATIVE = {
    "ar": "العربية", "de": "Deutsch", "en": "English", "es": "Español", "fr": "Français",
    "hi": "हिन्दी", "it": "Italiano", "ja": "日本語", "ko": "한국어", "nl": "Nederlands",
    "pl": "Polski", "pt": "Português", "ru": "Русский", "sv": "Svenska", "tr": "Türkçe",
    "uk": "Українська", "zh": "中文",
}
FLAGS = {
    "ar": "🇸🇦", "de": "🇩🇪", "en": "🇬🇧", "es": "🇪🇸", "fr": "🇫🇷", "hi": "🇮🇳", "it": "🇮🇹",
    "ja": "🇯🇵", "ko": "🇰🇷", "nl": "🇳🇱", "pl": "🇵🇱", "pt": "🇵🇹", "ru": "🇷🇺", "sv": "🇸🇪",
    "tr": "🇹🇷", "uk": "🇺🇦", "zh": "🇨🇳",
}

WEEK = 7
# How much history the card's chart shows, counted back from today. A rolling month: the
# report is about a week, and a curve drawn over years spends its whole width on the part
# nobody is asking about.
CHART_DAYS = 30
CARD_W, CARD_H = 1000, 700  # CSS pixels; the screenshot is this at 2× (report_card.html)


# ── numbers ──────────────────────────────────────────────────────────────────


def _count(n):
    return f"{n:,}"


def _short(n):
    """Axis labels: 7000 → 7k, 3500 → 3.5k, 480 → 480."""
    if n < 1000:
        return f"{n:g}"
    thousands = n / 1000
    return f"{thousands:g}k" if thousands == int(thousands) else f"{thousands:.1f}k"


def _axis(top, steps=3):
    """The smallest round ceiling at or above `top`, and the gridlines up to it.

    Three intervals, not two: a ceiling reached in two steps has to round 6,785 up to
    10,000, and a curve drawn into the bottom two thirds of its own plot looks like less
    progress than it is. Three lets 6,785 sit under 7,500 instead. Few gridlines either
    way — this is a shape read at arm's length on a phone, and every number that matters
    is direct-labelled on the mark.
    """
    if top <= 0:
        return 1, [0, 1]
    base = 10 ** math.floor(math.log10(top))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * base
        if math.ceil(top / step) <= steps:
            break
    count = math.ceil(top / step)
    return step * count, [step * i for i in range(count + 1)]


def _ago(days):
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


# ── the week ─────────────────────────────────────────────────────────────────


def week_bounds(today, back=0):
    """The last **complete calendar week**, Monday to Sunday, both ends inclusive.

    It used to be the seven days ending today, on the reasoning that the report fires on
    whatever day the schedule fires and "the last seven days" is the span the reader just
    lived. The user reversed that (2026-08-01) and the reason is `catchup`: the schedule is
    Monday morning, but a Mac asleep or a report-host restart can push a run to Wednesday, and a
    window counted back from *today* would then quietly report Wed-to-Wed — a different week
    every time it slipped, with two days counted twice and two never reported at all.

    Anchoring to the calendar makes a late run report the *same* week a punctual one would.
    So: the most recent Sunday strictly before today, and the Monday six days before it.
    Fired Monday, that is the week that ended yesterday; fired Wednesday, it is still that
    week. `back` steps whole weeks further into the past.
    """
    # Monday is weekday 0, so `weekday() + 1` days back is always the previous Sunday —
    # from a Monday that is yesterday, from a Sunday it is the Sunday before, never today.
    end = today - dt.timedelta(days=today.weekday() + 1 + WEEK * back)
    return end - dt.timedelta(days=WEEK - 1), end


def _cumulative(dates, start, days):
    """Daily running totals over `days` from `start` — the line the card draws."""
    deltas = [0] * days
    for date in dates:
        index = (date - start).days
        if 0 <= index < days:
            deltas[index] += 1
    running, out = 0, []
    for delta in deltas:
        running += delta
        out.append(running)
    return out


def _day_ticks(start, days, count=5):
    """Evenly spaced day labels across the window, always including both ends.

    The chart used to run from the first word ever, where month names were the only labels
    that fit; a rolling month is short enough to label by date and short enough that the
    ends matter — the right-hand one is today.
    """
    if days < 2:
        return [{"i": 0, "label": start.strftime("%-d %b")}]
    at = sorted({round(i * (days - 1) / (count - 1)) for i in range(count)})
    return [{"i": i, "label": (start + dt.timedelta(days=i)).strftime("%-d %b")} for i in at]


def build(words, langs=(), weeks=0, today=None):
    """Everything both the cards and the message say, computed once.

    Nothing downstream re-derives a number: the card renders what is in here and the
    message quotes it, so the picture and the text can't disagree about the same week —
    the failure `plots._kanji` avoids by reusing `kanji.collect()`, arriving here through
    two renderers instead of two commands.
    """
    if not words:
        raise UserError("No words to report on. Run `gigaku import` first.")

    today = today or dt.date.today()
    weeks = weeks or settings.REPORT_WEEKS
    present = sorted({w.language for w in words})
    # Which languages are *in progress* is a judgement the project already made once, in
    # PLOTS_HIDDEN_LANGS — English is a 10k dump and French is finished, so neither is news.
    # The report reuses it rather than inventing a second list that could drift from it.
    default = [lang for lang in present if lang not in settings.hidden_langs()]
    langs = [lang for lang in (langs or settings.report_langs() or default) if lang in present]
    if not langs:
        raise UserError(
            f"No languages left to report on — the cache has {', '.join(present)} and "
            f"PLOTS_HIDDEN_LANGS/REPORT_LANGS leave nothing."
        )

    slots = _slots(present)
    start, end = week_bounds(today)

    # One coordinate system for every language, not one chart each: a shared x running from
    # the earliest word anybody has to today, and a shared y ceiling over the largest total.
    # Curves that don't share an axis are separate charts wearing one frame, and the point
    # of drawing them together (user's decision, 2026-08-01) is that they are comparable.
    dates = {lang: [w.date.date() for w in words
                    if w.language == lang and w.stage == Stage.KNOWN]
             for lang in langs}
    seen = [d for known in dates.values() for d in known]
    # A rolling month, not the whole history (user's decision, 2026-08-01). The full curve
    # spent nine tenths of its width on years the report is not about and squeezed the week
    # it *is* about into the last few pixels. `CHART_DAYS` back from today, clamped to the
    # history that exists, so a fresh cache draws what it has instead of a flat run-in.
    first = min(seen, default=today)
    origin = max(first, today - dt.timedelta(days=CHART_DAYS - 1))
    days = (today - origin).days + 1

    out, series = [], []
    for lang in langs:
        mine = [w for w in words if w.language == lang]
        known = dates[lang]

        buckets = []
        for back in reversed(range(weeks)):
            lo, hi = week_bounds(today, back)
            buckets.append({
                "start": lo.isoformat(),
                "label": lo.strftime("%-d %b"),
                "count": sum(1 for d in known if lo <= d <= hi),
            })

        # From the shared origin, so a language that started later simply runs along zero
        # until its first word instead of being drawn on an axis of its own. The window is a
        # month, but the curve is still the *total* — everything learnt before it is the
        # baseline the month is added to, or the chart would claim the count restarted.
        base = sum(1 for d in known if d < origin)
        values = [base + v for v in _cumulative(known, origin, days)]
        last = max((w.date.date() for w in mine), default=None)
        learning = sum(1 for w in mine if w.stage == Stage.LEARNING)

        color = PALETTE[slots[lang] % len(PALETTE)]
        series.append({
            "code": lang,
            "name": LANG_NAMES.get(lang, lang.upper()),
            "color": color,
            "values": values,
            # The endpoint is direct-labelled, so the total is a string the page prints
            # rather than a number it formats — the one rounding lives in build().
            "end": _count(len(known)),
        })
        out.append({
            "code": lang,
            "name": LANG_NAMES.get(lang, lang.upper()),
            "native": NATIVE.get(lang, ""),
            "flag": FLAGS.get(lang, "📘"),
            "color": color,
            "total": len(known),
            "learning": learning,
            "week": buckets[-1]["count"],
            # Formatted once, here: the card prints this string in its header and the
            # message prints it in a bullet, so the two cannot round it differently.
            "week_text": f"+{_count(buckets[-1]['count'])}",
            "prev": buckets[-2]["count"] if len(buckets) > 1 else 0,
            "last": last.isoformat() if last else "",
            "last_ago": _ago((today - last).days) if last else "never",
            "weeks": buckets,
        })

    ceiling, ticks = _axis(max((s["values"][-1] for s in series), default=0))
    return {
        "today": today.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "span": _span(start, end),
        "languages": out,
        "chart": {
            "start": origin.isoformat(),
            "ceiling": ceiling,
            "ticks": [{"v": t, "label": _short(t)} for t in ticks],
            "xticks": _day_ticks(origin, days),
            # The shaded band is the reported week's real position in the window, not
            # "the last seven days" — the week now ends before today, and after a late
            # catchup run it can end several days before it.
            "week_from": max(0, (start - origin).days),
            "week_to": min(days - 1, (end - origin).days),
            "series": series,
        },
    }


def _span(start, end):
    """`25–31 Jul 2026`, or `28 Jun – 4 Jul 2026` when the week straddles two months."""
    if start.month == end.month:
        return f"{start.day}–{end.day} {end:%b %Y}"
    return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"


# ── the cards ────────────────────────────────────────────────────────────────


def render_card(data, out_path):
    """The whole week as one page — a 1000×700 panel per language, stacked."""
    pages.render(CARD, out_path, {"__DATA__": pages.json_payload(data)})
    return out_path


def _complete(path):
    """Does this file end with a PNG's own terminator?

    `IEND` + its CRC are the last twelve bytes of every valid PNG, so this is the file
    saying it is finished — not a size that stopped growing, which a slow disk fakes.
    """
    try:
        with open(path, "rb") as f:
            f.seek(-8, os.SEEK_END)
            return f.read(4) == b"IEND"
    except OSError:
        return False


def shot(html_path, png_path, width=CARD_W, height=CARD_H, timeout=90):
    """Render a local page to PNG with headless Chrome.

    Chrome writes the screenshot and then **does not exit** — measured on this machine with
    both `--headless=old` and `--headless=new`: the PNG is complete on disk in about a
    second and the process is still alive 45 seconds later. So the process is never waited
    on. The *file* is the signal, `_complete` is the receipt, and the process is killed the
    moment the receipt is in. Waiting on the exit code instead would hang every run.

    It gets a scratch profile of its own: the user's Chrome is running (this whole project
    drives it), and a second Chrome on the same profile directory refuses to start.
    """
    chrome = settings.CHROME_APP
    if not os.path.exists(chrome):
        raise UserError(f"Chrome not found at {chrome} — set GIGAKU_CHROME_APP.")
    if os.path.exists(png_path):
        os.remove(png_path)

    profile = os.path.join(settings.REPORT_DIR, "chrome-profile")
    process = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", f"--screenshot={png_path}",
         f"--window-size={width},{height}", "--force-device-scale-factor=2",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--disable-extensions", "--disable-background-networking",
         "--disable-crash-reporter", "--hide-scrollbars", "--virtual-time-budget=4000",
         f"file://{html_path}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _complete(png_path):
                return png_path
            time.sleep(0.25)
        raise UserError(f"headless Chrome wrote no complete PNG in {timeout}s: {png_path}")
    finally:
        process.kill()
        process.wait()


def cards(data, directory=""):
    """One PNG for the whole week — every language stacked, in the message's order.

    It was one file per language sent as an album until the user asked for a single
    notification (2026-08-01). Stacking happens in the page, so the merge costs one
    screenshot of a taller window rather than a bitmap library to paste PNGs together.
    Still returns a list: `send` and `site` take images, and one is a list of one.
    """
    directory = directory or settings.REPORT_DIR
    os.makedirs(directory, exist_ok=True)
    html_path = os.path.join(directory, "card.html")
    png_path = os.path.join(directory, "card.png")
    render_card(data, html_path)
    shot(html_path, png_path)
    note(f"Drew {', '.join(l['name'] for l in data['languages'])} → "
         f"{os.path.basename(png_path)} ({os.path.getsize(png_path) // 1024} KB)")
    return [png_path]


# ── the message ──────────────────────────────────────────────────────────────


def message(data):
    """The caption the card travels with — HTML, and every number already in `build`.

    The layout is a familiar weekly-post shape: a bold title, then one blank-line-separated block per subject — a bold
    heading with `▸` facts under it. One fact per bullet, because that is what a bullet is
    for; `known · this week` on one line was two facts wearing one.
    """
    title = "📚 Vocabulary"
    if settings.PUBLISH_URL:
        # The link rides on the title rather than on a footer line of its own — the site is
        # reachable, and nothing has to be said to make it so.
        title = f'📚 <a href="{settings.PUBLISH_URL}">Vocabulary</a>'
    blocks = [f"<b>{title}</b>\n{data['span']}"]

    for language in data["languages"]:
        # This week, and nothing else. The week before, the four-week average and every
        # kanji number were each removed by the user, one at a time, over 2026-08-01: they
        # are history, and they compete with the one number the report is about. **Kanji do
        # not belong in this message** — asked for twice, don't put them back. `last word`
        # stays, and stays on *every* language rather than only the quiet ones: it is the
        # line that tells a +0 from a broken pipeline, and it can only be read that way if
        # it is always there to read.
        rows = [f"▸ {_count(language['total'])} known",
                f"▸ <b>{language['week_text']}</b> last week"]
        if language["last"]:
            rows.append(f"▸ last word {language['last_ago']}")
        blocks.append(f"<b>{language['flag']} {language['name']}</b>\n" + "\n".join(rows))

    # No `backup: N days ago` line — removed by the user's decision (2026-08-01). It was the
    # check on the checker, and dropping it gives up exactly one case: the daily job silently
    # not running at all. A failed, refused or stale run still alerts from `backup._alert`.
    return "\n\n".join(blocks)


# ── telegram ─────────────────────────────────────────────────────────────────


CAPTION_LIMIT = 1024


def _tg_len(text):
    """Visible length as Telegram counts it: tags stripped, UTF-16 code units."""
    return len(re.sub(r"<[^>]+>", "", text).encode("utf-16-le")) // 2


def send(token, chat, text, images):
    """One photo carrying the message as its caption — one notification, not two.

    The album-then-message shape came first, because Telegram caps a caption at 1024
    characters (an album's, at one item's) and the message used to be longer than that was
    worth risking. Both ends of that moved: the cards are now a single stacked PNG and the
    message is a dozen short lines, so the caption fits with room to spare — and the user
    asked for one message. The old shape is kept as the fallback rather than deleted: a
    caption that ever outgrew the limit would otherwise be a rejected send, and the check
    costs one line.
    """
    if images:
        one = len(images) == 1  # a one-item media group is rejected outright
        fields = {"chat_id": chat}
        if one and _tg_len(text) <= CAPTION_LIMIT:
            fields |= {"caption": text, "parse_mode": "HTML"}
        if one:
            result = tg_call(token, "sendPhoto", fields,
                             {"photo": (os.path.basename(images[0]),
                                        open(images[0], "rb").read())})
        else:
            media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(images))]
            files = {f"p{i}": (os.path.basename(p), open(p, "rb").read())
                     for i, p in enumerate(images)}
            result = tg_call(token, "sendMediaGroup",
                             fields | {"media": json.dumps(media)}, files)
        if not result.get("ok"):
            raise UserError(f"Telegram refused the cards: {result.get('description')}")
        if "caption" in fields:
            return result

    result = tg_call(token, "sendMessage", {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    })
    if not result.get("ok"):
        raise UserError(f"Telegram refused the message: {result.get('description')}")
    return result


# ── the site page ────────────────────────────────────────────────────────────


def site(data, images, text, directory=None, today=None):
    """The week as a page under REPORT_SITE_DIR: `<date>.html` per week, `index.html` the
    latest — `gigaku publish` copies the whole directory next to the chart page.

    The page is the same cards Telegram got, inlined as data: URIs (self-contained like
    every gigaku page; ~120 KB a card, a decision not a leak), plus the same message —
    the picture and the text still cannot disagree, because both are `build()`'s numbers.
    The archive is re-derivable when this cache dir dies: vocab-backup holds words.json
    per day, and `gigaku report --from-backup` re-renders any week of it.
    """
    directory = os.path.expanduser(directory or settings.REPORT_SITE_DIR)
    os.makedirs(directory, exist_ok=True)
    stamp = (today or dt.date.today()).isoformat()

    cards_html = "\n".join(
        '<img alt="Weekly chart card" src="data:image/png;base64,'
        + base64.b64encode(open(path, "rb").read()).decode() + '">'
        for path in images
    )
    weeks = sorted(
        (f for f in os.listdir(directory) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.html", f)),
        reverse=True,
    )
    if f"{stamp}.html" not in weeks:
        weeks.insert(0, f"{stamp}.html")
    archive = "\n".join(f'<li><a href="{week}">{week[:-5]}</a></li>' for week in weeks)

    subs = {
        "__BASE_CSS__": pages.BASE_CSS,
        "__SPAN__": data["span"],
        "__CARDS__": cards_html,
        # Already HTML — the same <b>/<a> subset the Telegram message uses.
        "__MESSAGE__": text,
        "__ARCHIVE__": archive,
    }
    pages.render(SITE_PAGE, os.path.join(directory, f"{stamp}.html"), subs)
    pages.render(SITE_PAGE, os.path.join(directory, "index.html"), subs)
    note(f"Week page → {directory}/{stamp}.html (+index, {len(weeks)} week(s) archived)")
    return directory


# ── the command ──────────────────────────────────────────────────────────────


def _words(from_backup):
    from lib.vocab.words import _from_json, import_words

    if not from_backup:
        return import_words()  # non-consuming, exactly as `publish` and `backup` refresh
    path = os.path.join(settings.BACKUP_DIR, "words.json")
    if not os.path.exists(path):
        raise UserError(f"No backed-up word list at {path} — run `gigaku backup` first.")
    with open(path, encoding="utf-8") as f:
        return [_from_json(item) for item in json.load(f)]


def main(post=False, weeks=0, langs=(), from_backup=False):
    data = build(_words(from_backup), langs=langs, weeks=weeks)
    text = message(data)
    images = cards(data)
    # The site page is written in dry runs too: the next `gigaku publish` should carry
    # the freshest week whether or not this run also posted it.
    site(data, images, text)

    if not post:
        note("Dry run — pass --send to post it.")
        print(text)
        return

    token, chat = credentials()
    send(token, chat, text, images)
    note(f"Sent {len(images)} card(s) + the week to Telegram.")
