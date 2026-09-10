"""Mark titles as anime or Korean, from Netflix's own per-title genre labels.

Neither source we already have can answer this. `itemSummary` carries no language or country,
and IMDb's `title.basics` has no country/language column either (the file that does,
`title.akas`, is half a gigabyte and answers a different question — a popular film has a
Korean aka without being Korean). Guessing from IMDb genre strings is worse than useless here:
IMDb calls Rick and Morty "Animation".

Netflix does know, and says so per title: `videos/<id>/genres` returns its own genre rows, and
they carry the **original language** alongside the taste labels —

    Dark               → German · Sci-Fi Shows · TV Mysteries
    All of Us Are Dead → Korean · Teen TV Shows · K-Dramas based on Webtoon
    Sword Art Online   → Anime Series · Action Anime · Japanese
    One Piece          → TV Shows Based on Manga · Fantasy TV Shows
    Rick and Morty     → Sitcoms · Sci-Fi Shows · TV Comedies

so a keyword rule over those names is Netflix's own answer rather than an inference.

Two things this replaced, both dead ends worth not re-walking:

* **Genre galleries are storefronts, not the catalogue.** Ripping gallery 7424 ("Anime") and
  eight anime sub-genres yielded 19–218 titles each and *still* missed One Piece. Per-title
  genres have no such ceiling.
* **`videos/<id>/genres` looked forbidden and is not.** A first pass probed 25 fields in one
  burst, and the endpoint's rate limiter refused most of them — indistinguishable from "not
  whitelisted". Falcor calls here are therefore spaced and retried, never batch-probed.

The names themselves are kept on each record (`nf_genres`): they are richer than IMDb's
("K-Dramas based on Webtoon"), and keeping them means a rule change costs no refetch.
"""
import os
import re
import time

from lib.config import note, settings
from lib.netflix import gallery, store
from lib.platform.chrome import Page, PageError, TabClosed

# name → (label shown on the page, what a Netflix genre name has to look like)
MARKERS = {
    "anime": ("Anime", re.compile(r"\banime\b|\bmanga\b", re.I)),
    "korean": ("Korean", re.compile(r"\bkorean\b|k-drama", re.I)),
}

# Any Netflix browse page carries the falcor client, so this doesn't need the gallery's own
# tab — but it does need some tab, and this is the least surprising order.
TAB_MATCHES = ("netflix.com/browse", "netflix.com/")


def cache_path():
    return os.path.join(os.path.expanduser(settings.TITLES_DIR), "nf-genres.json")


def read_cache():
    data = store.read_json(cache_path()) or {}
    return {str(k): v for k, v in (data.get("genres") or {}).items()}


def _page(preferred=None):
    for match in ([preferred] if preferred else []) + list(TAB_MATCHES):
        page = Page(match, error=PageError)
        try:
            if page.tab("") == "ok":
                return page
        except Exception:  # noqa: BLE001 - a window closing mid-probe is not an error here
            continue
    return None


def parse_genres(envelope):
    """Falcor envelope → {video id: [genre names]}. Pure, so the tests drive it directly."""
    videos = (envelope or {}).get("json", {}).get("videos", {})
    out = {}
    for key, value in videos.items():
        if key.startswith("$") or not isinstance(value, dict):
            continue
        rows = value.get("genres") or {}
        names = [rows[i].get("name") for i in sorted((k for k in rows if k.isdigit()), key=int)
                 if isinstance(rows.get(i), dict) and rows[i].get("name")]
        out[str(key)] = names
    return out


def fetch(page, ids, batch=None, chunk=None, delay=None):
    """Netflix's genre names for every id, asked in batches. Returns {id: [names]}."""
    batch = batch or max(1, settings.TITLES_GENRE_BATCH)
    chunk = chunk or max(1024, settings.TITLES_CHUNK_BYTES)
    delay = settings.TITLES_REQUEST_DELAY_MS / 1000 if delay is None else delay

    found = {}
    ids = list(ids)
    for start in range(0, len(ids), batch):
        group = ids[start:start + batch]
        path = ["videos", [int(i) for i in group], "genres", {"from": 0, "to": 9},
                ["id", "name"]]
        desc = f"genres {start + 1:,}–{start + len(group):,} of {len(ids):,}"
        envelope = gallery.retrying(
            lambda p=path, s=start: gallery.fetch_path(page, f"g{s}", p, chunk, desc), desc)
        found.update(parse_genres(envelope))
        # A title with no genre rows must still be recorded, or every run re-asks for it.
        for i in group:
            found.setdefault(str(i), [])
        if start and start // batch % 10 == 0:
            note(f"  {len(found):,} of {len(ids):,} titles…")
        if delay:
            time.sleep(delay)
    return found


def classify(names):
    """Which markers a genre-name list earns."""
    joined = " · ".join(names or [])
    return {name for name, (_, pattern) in MARKERS.items() if pattern.search(joined)}


def collect(records, preferred_match=None, refresh=False):
    """Genre names for every record, from cache plus whatever still has to be asked for."""
    cached = {} if refresh else read_cache()
    wanted = [str(r["nf_id"]) for r in records if r.get("nf_id")]
    missing = [i for i in wanted if i not in cached]
    if not missing:
        return cached

    page = _page(preferred_match)
    if page is None:
        note(f"Marking: no Netflix tab open — {len(missing):,} titles stay unmarked "
             "(open the gallery and re-run).")
        return cached

    note(f"Marking: reading Netflix's genres for {len(missing):,} titles…")
    try:
        cached.update(fetch(page, missing))
    except (PageError, TabClosed, OSError) as e:
        note(f"Marking: stopped early ({e}) — keeping what was read.")
    store.write_json(cache_path(), {"fetched_at": time.time(), "genres": cached})
    return cached


def apply(records, genres):
    """Attach nf_genres and the is_<marker> flags. Returns the count per marker."""
    counts = dict.fromkeys(MARKERS, 0)
    for rec in records:
        names = genres.get(str(rec.get("nf_id"))) or []
        rec["nf_genres"] = names
        marks = classify(names)
        for name in MARKERS:
            rec[f"is_{name}"] = name in marks
            counts[name] += bool(rec[f"is_{name}"])
    return counts
