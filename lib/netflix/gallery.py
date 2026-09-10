"""Rip a whole Netflix browse gallery by driving the page's own API client.

Netflix's browse pages talk to `/api/shakti/…/pathEvaluator` (falcor). We do **not** rebuild
that request: `netflix.appContext.state.pathEvaluator` is a live falcor Model sitting in the
page's MAIN world, and it already knows the endpoint, the build identifier, the auth token and
the ESN. Asking it for a window of the gallery is one call:

    pathEvaluator.get(['genres', 81510840, 'su', {from: 0, to: 49}, FIELDS])

Four measured facts shape everything here:

* **The endpoint runs a path whitelist.** Only these leaves resolve for a gallery slot —
  ``itemSummary``, ``availability``, ``episodeCount``, ``summary``, ``queue``,
  ``inRemindMeList``. The rich per-video leaves (``synopsis``, ``genres``, ``cast``,
  ``runtime``, ``tags``) are rejected on ``videos/<id>``, and **one** unknown leaf fails the
  *whole* request with "xhr error", not just that field. So FIELDS is a closed set: don't add
  to it hopefully. Genres and runtime come from IMDb instead (lib/netflix/imdb.py).
* **Titles come back in English** even on a `/de` gallery ("All of Us Are Dead", not
  "Wir sind die Welle"), which is what makes the IMDb join tractable.
* **Past the end, slots materialise empty rather than erroring** — index 4001 has a title,
  4400 is an empty object. That's the stop condition, and it's why the rip needs no total.
* **A burst of parallel requests gets rate-limited** and returns "xhr error" for a window
  that succeeds moments later. Windows therefore go one at a time, with a delay, and a
  failed window is retried before it's believed.

``fetch`` is asynchronous and one AppleScript call cannot both start and await it, so every
window is *kick → poll → chunked read → free*, tagged with a per-window id so a stale buffer
can never be read as this window's answer.
"""
import json
import os
import re
import time

from lib.config import UserError, note, settings
from lib.netflix import store
from lib.platform.chrome import Page, PageError, TabClosed, wait_progress
from lib.platform.power import DisplayWakeLock

# The complete set of gallery-slot fields this endpoint accepts (see the module docstring).
FIELDS = ["itemSummary", "availability", "episodeCount", "summary", "queue", "inRemindMeList"]

# The genre node's lists. Measured 2026-07-28: the browse page only ever asks for `su`
# ("Suggestions For You"), but `az`, `za` and `yr` — the sort orders Netflix removed from the
# web UI — still resolve; `sr` and `ry` are refused. They are walked and merged by id so no
# single list decides the catalogue. On the German gallery they agreed to within eight titles,
# which settles a question worth settling: a title in none of them (My Hero Academia, which
# plays with a German audio track) is missing from the **node**, not from a personalised view
# of it. That is the node's problem, and TITLES_EXTRA_IDS is the answer to it.
SUBLISTS = ("su", "az", "za", "yr")

# Reach the falcor model, or report why not. Left in scope as `pe` for the bodies below.
_PE = (
    "var pe=null,perr='';"
    "try{pe=netflix.appContext.state.pathEvaluator;}catch(e){perr=''+e;}"
    "if(!pe){o.__nope=perr||'no pathEvaluator';}"
)


def parse_url(url):
    """`…/browse/audio/81510840/de` → ("81510840", "de", "netflix.com/browse/audio/81510840").

    The tab is matched by the gallery-specific path, never a bare "netflix.com": an open
    /watch tab (or a second gallery) must not be driven by accident.
    """
    match = re.search(r"netflix\.com/browse/(audio|subtitles)/(\d+)(?:/([a-zA-Z-]+))?", url)
    if not match:
        raise UserError(
            f"Not a Netflix browse-gallery URL: {url}\n"
            "Expected something like https://www.netflix.com/browse/audio/81510840/de"
        )
    kind, genre_id, lang = match.group(1), match.group(2), (match.group(3) or "")
    return genre_id, lang, f"netflix.com/browse/{kind}/{genre_id}"


def dump_path(genre_id, lang):
    return os.path.join(os.path.expanduser(settings.TITLES_DIR),
                        f"gallery-{genre_id}{'-' + lang if lang else ''}.json")


# ── one window ───────────────────────────────────────────────────────────────

def _kick(page, tag, path):
    """Start the request; return immediately (the answer lands in a page global)."""
    path = json.dumps(path)
    body = (
        _PE +
        "if(pe){window.__gk=window.__gk||{};"
        f"var b=window.__gk['{tag}']={{s:'pending'}};"
        f"pe.get({path}).then(function(r){{"
        "try{b.buf=JSON.stringify(r);b.n=b.buf.length;b.s='done';}"
        "catch(e){b.s='err';b.msg='stringify: '+e;}},"
        "function(e){b.s='err';b.msg=''+((e&&(e.message||e.toString()))||'unknown');});"
        "o.kicked=1;}"
    )
    result = page.main(body)
    if result.get("__nope"):
        raise PageError(f"no falcor client on the page ({result['__nope']}) — is it a Netflix "
                        "browse page, fully loaded?")
    if result.get("__err"):
        raise PageError(f"kick failed: {result['__err']}")
    if not result.get("kicked"):
        raise PageError(f"kick did not run (page mid-navigation?): {result}")


def _await(page, tag, desc):
    """Block until the request settles. Returns the buffer length, or raises PageError."""
    done = wait_progress(
        lambda: page.main(f"var b=(window.__gk||{{}})['{tag}']||{{}};"
                          "o.s=b.s;o.n=b.n;o.msg=b.msg;"),
        lambda d: d.get("s") in ("done", "err"),
        lambda d: (d.get("s"), d.get("n")),
        desc, interval=0.4, giveup=45, abort=page.closed, error=PageError,
    )
    if done.get("s") == "err":
        raise PageError(f"{desc}: falcor said {done.get('msg')!r}")
    return int(done.get("n") or 0)


def _read(page, tag, total, chunk):
    """Read the JSON back in slices and verify the length — AppleScript returns a *string*,
    and a silently truncated one would parse as a short-but-valid gallery."""
    parts, offset = [], 0
    while offset < total:
        got = page.main(f"var b=(window.__gk||{{}})['{tag}']||{{}};var s=b.buf||'';"
                        f"o.p=s.substr({offset},{chunk});o.n=s.length;")
        piece = got.get("p")
        if piece is None:
            raise PageError(f"chunk at {offset} came back empty (buffer gone?)")
        if int(got.get("n") or 0) != total:
            raise PageError(f"buffer changed under us ({got.get('n')} != {total})")
        parts.append(piece)
        offset += len(piece)
        if not piece:
            raise PageError(f"zero-length chunk at {offset} — cannot make progress")
    joined = "".join(parts)
    if len(joined) != total:
        raise PageError(f"reassembled {len(joined)} chars, expected {total}")
    return joined


def _free(page, tag):
    try:
        page.main(f"try{{delete window.__gk['{tag}'];}}catch(e){{}}o.k=1;")
    except (PageError, TabClosed):
        pass  # freeing is hygiene, never a reason to fail a rip


def parse_window(envelope, genre_id, sublist="su"):
    """Falcor envelope → {index: record}. Pure, so the tests drive it from a saved fixture."""
    su = (envelope or {}).get("json", {}).get("genres", {}).get(str(genre_id), {}).get(sublist, {})
    out = {}
    for key, entry in su.items():
        if not key.isdigit():
            continue  # falcor adds $__path and friends
        rec = store.record(int(key), entry)
        if rec:
            out[int(key)] = rec
    return out


def fetch_path(page, tag, path, chunk, desc):
    """Ask the page's falcor client for ``path`` and return the parsed jsonGraph.

    The whole kick → poll → chunked read → free cycle, shared by the gallery windows and the
    per-title genre lookups in lib/netflix/markers.py.
    """
    _kick(page, tag, path)
    total = _await(page, tag, desc)
    if not total:
        _free(page, tag)
        raise PageError(f"{desc}: falcor returned nothing")
    raw = _read(page, tag, total, chunk)
    _free(page, tag)
    try:
        return json.loads(raw)
    except ValueError as e:
        raise PageError(f"{desc}: unparseable response ({e})")


def retrying(call, desc, attempts=3):
    """Run a falcor call, retrying it: the endpoint rate-limits bursts and refuses a request
    that succeeds moments later, so one "xhr error" is never taken at face value. This is the
    trap that made a first pass conclude `videos/<id>/genres` was not whitelisted — it is."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except PageError as e:
            last = e
            backoff = 2 * attempt
            note(f"  {desc} failed ({e}) — retrying in {backoff}s")
            time.sleep(backoff)
    raise UserError(f"{desc} kept failing: {last}")


def window(page, genre_id, start, count, chunk, sublist="su"):
    """One window of the gallery, as {index: record}. Empty dict = past the end."""
    path = ["genres", int(genre_id), sublist,
            {"from": start, "to": start + count - 1}, FIELDS]
    envelope = fetch_path(page, f"w{sublist}{start}", path, chunk,
                          f"{sublist} {start}–{start + count - 1}")
    return parse_window(envelope, genre_id, sublist)


def merge(records, found, sublist):
    """Fold one window's slots into the by-id store, remembering which list found them.

    Keyed by Netflix id rather than by slot index, because the same title sits at a different
    index in every list. ``rank`` keeps `su`'s index when `su` has one — it is the page's
    sort tiebreak, and the storefront's own order is the meaningful one.
    """
    for index, rec in sorted(found.items()):
        key = str(rec.get("nf_id"))
        have = records.get(key)
        if have is None:
            rec["sublists"] = [sublist]
            records[key] = stamp(rec)
            continue
        if sublist not in have["sublists"]:
            have["sublists"].append(sublist)
            stamp(have)
        if sublist == "su":
            have["rank"] = index
    return records


def stamp(rec):
    """Set ``discovery``: empty for a title the storefront lists, otherwise where it came
    from. That is the flag the page turns into a NEW pill — "the gallery did not show me
    this" is exactly the thing worth seeing."""
    lists = rec.get("sublists") or []
    rec["discovery"] = "" if "su" in lists else (",".join(lists) or "manual")
    return rec


# ── the whole gallery ────────────────────────────────────────────────────────

def walk(page, genre_id, records, start, cap, *, size, chunk, delay, on_window=None,
         sublist="su", fold=None):
    """Page through a gallery from ``start`` until it runs out or hits ``cap``.

    Shared by the main rip and the marker galleries (lib/netflix/markers.py) so the paging
    contract — 50 at a time, stop on the first all-empty window, retry a rate-limited one —
    exists once. ``fold`` decides how a window joins ``records``; markers.py keeps the plain
    index-keyed dict, the rip folds by id via ``merge``. Returns (next_index, complete).
    """
    complete = False
    while start < cap:
        count = min(size, cap - start)
        got = _window_with_retry(page, genre_id, start, count, chunk, sublist)
        if not got:
            complete = True
            break
        if fold:
            fold(records, got, sublist)
        else:
            records.update(got)
        start += count
        if on_window:
            on_window(records, start)
        if delay:
            time.sleep(delay)
    return start, complete


def adopt(dump):
    """An older dump → the by-id shape, in place.

    The first rip only knew `su`, so its records carry neither ``sublists`` nor
    ``discovery``: they are the storefront's, which is exactly what "not new" means. Done
    once, automatically, like the word cache's adoption in lib/vocab/words.py — a dump from
    before this change must not force a re-rip of 4,000 titles.
    """
    records, progress = {}, dict(dump.get("progress") or {})
    if "su" not in progress and dump.get("next_from") is not None:
        progress["su"] = dump["next_from"]
    for rec in dump.get("records") or []:
        rec.setdefault("sublists", ["su"])
        records[str(rec.get("nf_id"))] = stamp(rec)
    done = set(dump.get("done") or ([] if not dump.get("complete") else ["su"]))
    return records, progress, done


def rip(url=None, limit=None, refresh=False):
    """Rip every title in the gallery. Resumable, and cached between runs.

    Walks each of TITLES_SUBLISTS and merges them by Netflix id, then pulls in
    TITLES_EXTRA_IDS. Returns the dump dict: {url, genre_id, lang, name, complete, progress,
    done, records}.
    """
    url = url or settings.TITLES_URL
    genre_id, lang, tab_match = parse_url(url)
    path = dump_path(genre_id, lang)
    sublists = settings.sublists() or ["su"]
    extras = settings.extra_ids()

    dump = None if refresh else store.read_json(path)
    records, progress, done = adopt(dump) if dump else ({}, {}, set())
    if dump:
        age_days = (time.time() - dump.get("fetched_at", 0)) / 86400
        stale = age_days >= settings.TITLES_MAX_AGE_DAYS
        if stale:
            records, progress, done = {}, {}, set()  # a stale dump is re-walked, not topped up
        elif not limit and done.issuperset(sublists) and _has_extras(records, extras):
            note(f"Gallery cache is {age_days:.1f} days old — "
                 f"{len(dump['records']):,} titles, reusing it (--refresh to re-rip).")
            return dump

    todo = [name for name in sublists if name not in done]
    if records:
        note(f"{len(records):,} titles already in the dump; walking {', '.join(todo) or 'nothing'}.")

    page = Page(tab_match, error=PageError)
    size = max(1, settings.TITLES_PAGE_SIZE)
    chunk = max(1024, settings.TITLES_CHUNK_BYTES)
    delay = max(0, settings.TITLES_REQUEST_DELAY_MS) / 1000

    if page.tab("") != "ok":
        raise UserError(
            f"No Chrome tab is on that gallery. Open {url} in Chrome (logged in) and re-run.\n"
            "Chrome ▸ View ▸ Developer ▸ 'Allow JavaScript from Apple Events' must be on."
        )

    def payload(complete):
        return {
            "url": url, "genre_id": genre_id, "lang": lang,
            "name": (dump or {}).get("name"),
            "fetched_at": time.time(), "complete": complete,
            "progress": progress, "done": sorted(done),
            # su's order first, then whatever the other lists added, so the dump reads like
            # the gallery with the extras appended rather than reshuffled.
            "records": sorted(records.values(),
                              key=lambda r: (bool(r.get("discovery")), r.get("rank") or 0)),
        }

    try:
        with DisplayWakeLock("gigaku titles"):
            for name in todo:
                start = progress.get(name, 0)
                cap = min(settings.TITLES_MAX, start + limit if limit else settings.TITLES_MAX)

                def save(found, next_from, _name=name):
                    progress[_name] = next_from
                    store.write_json(path, payload(False))
                    note(f"  {_name}: {len(found):,} titles … (next index {next_from:,})")

                start, complete = walk(page, genre_id, records, start, cap, size=size,
                                       chunk=chunk, delay=delay, on_window=save,
                                       sublist=name, fold=merge)
                progress[name] = start
                if complete:
                    done.add(name)
                    note(f"{name}: reached the end at index {start:,} "
                         f"({len(records):,} titles merged).")
                else:
                    note(f"{name}: stopped at the {cap:,}-title cap "
                         f"({'--limit' if limit else 'TITLES_MAX'}).")

            if extras:
                fresh = {k: v for k, v in fetch_ids(page, extras, chunk).items()
                         if k not in records}
                records.update(fresh)
                note(f"TITLES_EXTRA_IDS: {len(fresh):,} added by id "
                     f"({len(extras) - len(fresh):,} already in the gallery).")
    except TabClosed:
        raise UserError("The Netflix tab was closed — reopen it and re-run; the rip resumes "
                        "where it stopped.")

    final = payload(done.issuperset(sublists))
    store.write_json(path, final)
    new = sum(1 for r in records.values() if r.get("discovery"))
    note(f'Ripped {len(records):,} titles ({new:,} the storefront does not list) → "{path}"')
    return final


def _has_extras(records, extras):
    """Every named id is already in the dump — otherwise the cache is short of what was asked
    for and the rip has to go get them, fresh dump or not."""
    return all(str(i) in records for i in extras)


def _window_with_retry(page, genre_id, start, count, chunk, sublist="su"):
    return retrying(lambda: window(page, genre_id, start, count, chunk, sublist),
                    f"{sublist} window at {start:,}")


def fetch_ids(page, ids, chunk):
    """Records for titles named by id — the way in for what the node leaves out.

    Batched exactly like markers.py's genre lookups, and parsed by ``store.record`` because
    ``videos/<id>`` answers the same FIELDS a gallery slot does (measured). An id Netflix
    doesn't know simply comes back without an ``itemSummary`` and is reported, not guessed at.
    """
    out, missing = {}, []
    batch = max(1, settings.TITLES_GENRE_BATCH)
    for i in range(0, len(ids), batch):
        chunk_ids = [int(x) for x in ids[i:i + batch]]
        envelope = retrying(
            lambda: fetch_path(page, f"x{i}", ["videos", chunk_ids, FIELDS], chunk,
                               f"{len(chunk_ids)} extra titles"),
            f"extra titles {i + 1}–{i + len(chunk_ids)}")
        videos = (envelope or {}).get("json", {}).get("videos", {})
        for nf_id in chunk_ids:
            rec = store.record(0, videos.get(str(nf_id)) or {})
            if rec is None:
                missing.append(str(nf_id))
                continue
            rec["sublists"] = ["manual"]
            out[str(nf_id)] = stamp(rec)
        time.sleep(max(0, settings.TITLES_REQUEST_DELAY_MS) / 1000)
    if missing:
        note(f"  TITLES_EXTRA_IDS: Netflix returned nothing for {', '.join(missing)}.")
    return out


def probe(url=None):
    """What the page offers, for when a Netflix build changes and the rip stops working.

    Everything here is read-only and cheap. It answers, in order: is the tab there, does
    main-world injection work at all, is there a falcor client, and does one small window come
    back?
    """
    url = url or settings.TITLES_URL
    genre_id, lang, tab_match = parse_url(url)
    page = Page(tab_match, error=PageError)
    out = {"url": url, "genre_id": genre_id, "lang": lang, "tab_match": tab_match}
    out["tab"] = page.tab("")
    if out["tab"] != "ok":
        return out

    surface = page.main(
        _PE +
        "o.href=location.href;o.injected=1;o.falcor=!!pe;"
        "try{o.build=netflix.reactContext.models.serverDefs.data.BUILD_IDENTIFIER;}catch(e){}"
        "try{var g=netflix.falcorCache.genres;o.cachedGenres=Object.keys(g);"
        f"o.name=(g['{genre_id}']&&g['{genre_id}'].name&&g['{genre_id}'].name.value)||null;}}catch(e){{}}"
        "o.tiles=document.querySelectorAll('a[href*=\"/watch/\"]').length;"
    )
    out["surface"] = surface
    if not surface.get("falcor"):
        return out

    try:
        got = window(page, genre_id, 0, 2, settings.TITLES_CHUNK_BYTES)
        out["sample"] = [{k: v for k, v in r.items() if k != "raw"} for r in got.values()]
        out["itemSummary_fields"] = sorted(
            {k for r in got.values() for k in (r.get("raw") or {})}
        )
    except (PageError, TabClosed) as e:
        out["sample_error"] = str(e)
    out["fields_used"] = FIELDS
    return out
