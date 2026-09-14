"""`gigaku titles` — rip a Netflix browse gallery, join it to IMDb, and draw the result.

Three artifacts, because they answer different questions:

* the **JSON dump** (per gallery, in TITLES_DIR) keeps Netflix's ``itemSummary`` verbatim
  alongside the IMDb fields — the raw material for whatever you want to enrich next;
* the **CSV** is the same table flat, for a spreadsheet;
* the **HTML page** is the one you actually use: sort by weighted rating, filter to films
  with 10k+ votes, pick something.

The staging is deliberate. Ripping costs minutes of API calls, so a complete dump younger
than TITLES_MAX_AGE_DAYS is reused; the IMDb datasets are cached for IMDB_MAX_AGE_DAYS. That
makes a re-run with different ranking parameters a sub-second re-render, which is why this is
one command and not three — the expensive stages skip themselves.

Mirrors lib/vocab/plots.py's build/render/main shape, including the ``__DATA__`` sentinel.
"""
import os
import time
import webbrowser
from datetime import datetime

from lib.config import UserError, note, settings
from lib.netflix import store

PAGE = os.path.join(os.path.dirname(__file__), "titles_page.html")


def build(dump, stats):
    """The page payload. Drops ``raw`` — the dump keeps it; the page has no use for it and it
    would triple the file — and leaves out the genres you never want to see.

    The genre cut happens *here*, not in the rip: the dump and the CSV keep every title, so
    widening TITLES_HIDE_GENRES again costs a re-render rather than a re-rip.
    """
    records = dump.get("records") or []
    if not records:
        raise UserError("No titles to show. Run `gigaku titles` with a gallery URL first.")

    hidden = settings.hide_genres()
    kept = [rec for rec in records
            if not (hidden and {g.casefold() for g in (rec.get("imdb_genres") or [])} & hidden)]
    if hidden and len(kept) < len(records):
        note(f"Left {len(records) - len(kept):,} titles out of the page "
             f"({settings.TITLES_HIDE_GENRES}); they are still in the CSV and the dump.")
    if not kept:
        raise UserError(
            f"TITLES_HIDE_GENRES={settings.TITLES_HIDE_GENRES!r} excluded every title. "
            "Pass --titles-hide-genres '' to show them all."
        )

    rows = [{k: v for k, v in rec.items() if k != "raw"} for rec in kept]
    genres = sorted({g for rec in kept for g in (rec.get("imdb_genres") or [])})
    ripped = dump.get("fetched_at") or time.time()
    return {
        "name": dump.get("name") or "Netflix titles",
        "url": dump.get("url"),
        "lang": dump.get("lang"),
        # The page keys its Gigaku List (a browser-side shortlist) on this, so two galleries
        # rendered to the same path can't inherit each other's list.
        "genre_id": dump.get("genre_id"),
        "ripped": datetime.fromtimestamp(ripped).strftime("%Y-%m-%d"),
        "complete": bool(dump.get("complete")),
        "bayes": list(settings.imdb_bayes()),
        "genres": genres,
        "hidden_genres": [g.strip() for g in settings.TITLES_HIDE_GENRES.split(",") if g.strip()],
        "hidden_count": len(records) - len(kept),
        "stats": stats,
        "rows": rows,
    }


def render(data, out_path):
    from lib import pages

    pages.render(PAGE, out_path, {"__DATA__": pages.json_payload(data)})
    return out_path


def stats_for(records):
    rated = sorted(r["imdb_rating"] for r in records if r.get("imdb_rating"))
    return {
        "total": len(records),
        "matched": len(rated),
        "unmatched": len(records) - len(rated),
        "median": rated[len(rated) // 2] if rated else None,
        "great": sum(1 for r in rated if r >= 8.0),
        "my_list": sum(1 for r in records if r.get("in_my_list")),
        "watched": sum(1 for r in records if r.get("watched")),
        # Titles the gallery's own list never showed — see lib/netflix/gallery.py.
        "discovered": sum(1 for r in records if r.get("discovery")),
    }


def main(url=None, refresh=False, refresh_imdb=False, limit=None, imdb=True,
         watched=True, mark=True, as_tsv=False, open_browser=True):
    from lib.netflix import gallery

    dump = gallery.rip(url=url, limit=limit, refresh=refresh)
    records = dump.get("records") or []
    if not records:
        raise UserError("The gallery rip produced no titles — try `gigaku titles --probe`.")

    # The gallery's own name ("German Dubbed Movies & TV") titles the page. It rides in the
    # falcor cache rather than the rip, so take it once, here, and remember it in the dump.
    if not dump.get("name"):
        try:
            probe = gallery.probe(dump.get("url"))
            dump["name"] = (probe.get("surface") or {}).get("name")
        except Exception:  # noqa: BLE001 - a missing page title is not worth failing over
            dump["name"] = None

    if imdb:
        from lib.netflix import imdb as imdb_module

        imdb_module.enrich(records, refresh=refresh_imdb)

    # What you've already seen, straight from the Netflix Watched Marker extension. Applied
    # after the join so it lands in the dump and the CSV too, not just the page.
    from lib.netflix import watched as watched_module

    seen = watched_module.main(refresh_list=watched)
    watched_module.apply(records, seen)

    if mark:
        from lib.netflix import markers as markers_module

        _, _, tab_match = gallery.parse_url(dump["url"])
        counts = markers_module.apply(records, markers_module.collect(records, tab_match))
        if counts:
            note("Marked " + ", ".join(f"{n:,} {k}" for k, n in counts.items()) + ".")

    store.write_json(gallery.dump_path(dump["genre_id"], dump["lang"]), dump)

    stats = stats_for(records)
    note(f"{stats['matched']:,} of {stats['total']:,} titles have an IMDb rating "
         f"({stats['great']:,} at 8.0+, median {stats['median'] or '—'}); "
         f"{stats['watched']:,} already watched.")
    if stats["discovered"]:
        note(f"{stats['discovered']:,} of them are tagged NEW — the gallery's own list "
             "does not show them.")

    if as_tsv:
        print(store.tsv(records))
        return

    csv_path = store.write_csv(os.path.expanduser(settings.TITLES_CSV), records)
    data = build(dump, stats)
    path = render(data, os.path.expanduser(settings.TITLES_OUT))
    note(f'Wrote "{csv_path}" and "{path}"')
    if open_browser:
        webbrowser.open(f"file://{path}")
