"""Join the ripped Netflix list to IMDb ratings, using IMDb's own published datasets.

Why the datasets and not an API: they are official, complete, refreshed daily, need no key,
and impose no rate limit — https://datasets.imdbws.com/. The cost is size (`title.basics`
is ~225 MB gzipped, ~11.7 M rows), so nothing is ever held whole in memory: each file is
streamed once and **reduced to a slim derivative**, and the raw download is deleted. Steady
state on disk is tens of MB instead of a quarter-gigabyte.

Matching is deliberately explainable rather than clever. There is no fuzzy-match dependency;
there is a ladder of normalised title keys, a year window that knows a Netflix show's
"releaseYear" is often its *latest* season, and a type check. Every row records where its
match came from and how strongly it scored, so a wrong answer is visible in the output
instead of hiding behind a rating. Titles that match nothing keep null ratings and are listed
on the page's own Unmatched tab — the join never quietly drops a title.

For the residue, IMDb's unauthenticated suggestion endpoint (the one behind the search box)
resolves alternate titles server-side, which is exactly the case a local join is worst at
(anime released under a different English name, say). It is paced, capped and cached.
"""
import gzip
import json
import math
import os
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from lib.config import UserError, note, settings

DATASETS = "https://datasets.imdbws.com/"
SUGGEST_URL = "https://v2.sg.media-imdb.com/suggestion/t/{}.json"
UA = "gigaku/1.0 (personal vocabulary + watchlist tool)"

# Netflix says "movie" or "show"; IMDb has a dozen titleTypes. These are the compatible sets —
# a mismatch is the single strongest signal that a same-named candidate is the wrong title.
MOVIE_TYPES = {"movie", "tvMovie", "video", "short", "tvSpecial", "videoGame"} - {"videoGame"}
SHOW_TYPES = {"tvSeries", "tvMiniSeries"}

# Leading articles are dropped into a *separate, lower-weighted* key rather than removed
# outright: "The Office" and "Office" are different titles, but "Der Pate"/"Pate" should still
# be reachable when IMDb files it the other way round.
ARTICLES = ("the ", "a ", "an ", "der ", "die ", "das ", "ein ", "eine ", "le ", "la ", "les ")

# Trailing season/part markers Netflix adds and IMDb does not.
SEASON_WORDS = ("season", "staffel", "series", "part", "teil", "vol", "volume", "book",
                "chapter", "collection")


# ── normalisation ────────────────────────────────────────────────────────────

def norm(title):
    """A comparable form of a title: case-, accent- and punctuation-insensitive.

    NFKD splits accented letters into base + combining mark, so dropping the marks handles
    ä→a, é→e and friends in one step; ß has no decomposition and is spelled out explicitly.
    """
    if not title:
        return ""
    text = str(title).replace("ß", "ss").replace("&", " and ").replace("+", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = "".join(c if c.isalnum() else " " for c in text.casefold())
    return " ".join(text.split())


def _strip_season(key):
    """"stranger things season 4" → "stranger things"."""
    parts = key.split()
    while len(parts) >= 2 and parts[-1].isdigit() and parts[-2] in SEASON_WORDS:
        parts = parts[:-2]
    while len(parts) >= 2 and parts[-1] in SEASON_WORDS:
        parts = parts[:-1]
    return " ".join(parts)


def keys(title):
    """Every normalised key a title should be findable by → {key: weight}.

    Weights are how much of the title score that key earns: an exact whole-title hit is worth
    more than a hit after throwing part of the title away.
    """
    full = norm(title)
    if not full:
        return {}
    out = {full: 1.0}

    for sep in (":", " - ", " – ", " — "):
        if sep in str(title):
            head = norm(str(title).split(sep)[0])
            if head and len(head) >= 3:
                out.setdefault(head, 0.85)

    # Netflix disambiguates with a parenthetical the way IMDb doesn't: "The Office (U.S.)" is
    # filed as plain "The Office". Without this the local join misses it entirely and it has
    # to be bought back with a network lookup.
    if "(" in str(title):
        bare = norm(re.sub(r"\([^)]*\)", " ", str(title)))
        if bare and len(bare) >= 3:
            out.setdefault(bare, 0.85)

    stripped = _strip_season(full)
    if stripped and stripped != full and len(stripped) >= 3:
        out.setdefault(stripped, 0.82)

    for article in ARTICLES:
        if full.startswith(article):
            rest = full[len(article):]
            if len(rest) >= 3:
                out.setdefault(rest, 0.80)
            break
    return out


# ── datasets: download once, reduce, keep the slims ──────────────────────────

def _paths():
    root = os.path.expanduser(settings.IMDB_DIR)
    return {
        "root": root,
        "raw": os.path.join(root, "raw"),
        "basics": os.path.join(root, "basics.slim.tsv.gz"),
        "ratings": os.path.join(root, "ratings.tsv.gz"),
        "suggest": os.path.join(root, "suggest.json"),
    }


def _download(name, dest):
    """Stream a dataset to disk. Nothing in gigaku streamed a response to a file before this;
    the alternative — reading 225 MB into memory to write it out again — is not one."""
    url = DATASETS + name
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                total = int(response.headers.get("Content-Length") or 0)
                note(f"  downloading {name} ({total / 1e6:.0f} MB)…")
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(response, f, 1 << 20)
            os.replace(tmp, dest)
            return dest
        except (urllib.error.URLError, OSError) as e:  # noqa: PERF203 - transient network
            if os.path.exists(tmp):
                os.unlink(tmp)
            if attempt == 3:
                raise UserError(f"Could not download {url}: {e}")
            time.sleep(1 + attempt)


def _reduce_basics(raw, slim):
    """title.basics → the columns we use, minus the 8-million-odd tvEpisode rows.

    Episodes can never match a gallery entry (a Netflix slot is a film or a series, never a
    single episode) and they are two thirds of the file, so dropping them is what turns a
    225 MB dataset into a few tens of MB and a multi-minute scan into seconds.
    """
    kept = dropped = 0
    with gzip.open(raw, "rt", encoding="utf-8", errors="replace") as src, \
            gzip.open(slim, "wt", encoding="utf-8") as dst:
        header = src.readline().rstrip("\n").split("\t")
        col = {name: i for i, name in enumerate(header)}
        want = ["tconst", "titleType", "primaryTitle", "originalTitle",
                "startYear", "endYear", "runtimeMinutes", "genres"]
        missing = [w for w in want if w not in col]
        if missing:
            raise UserError(f"IMDb title.basics is missing columns {missing} — the dataset "
                            "format changed; lib/netflix/imdb.py needs updating.")
        dst.write("\t".join(want) + "\n")
        for line in src:
            row = line.rstrip("\n").split("\t")
            if len(row) < len(header):
                continue
            if row[col["titleType"]] == "tvEpisode":
                dropped += 1
                continue
            dst.write("\t".join(row[col[w]] for w in want) + "\n")
            kept += 1
    note(f"  basics: kept {kept:,} titles, dropped {dropped:,} episodes")
    return slim


def ensure(refresh=False):
    """Make sure the slim datasets exist and are fresh enough. Returns the paths."""
    paths = _paths()
    os.makedirs(paths["root"], exist_ok=True)

    fresh = all(os.path.exists(paths[k]) for k in ("basics", "ratings")) and not refresh
    if fresh:
        age = (time.time() - min(os.path.getmtime(paths[k]) for k in ("basics", "ratings"))) / 86400
        if age < settings.IMDB_MAX_AGE_DAYS:
            note(f"IMDb datasets are {age:.1f} days old — reusing them.")
            return paths
        note(f"IMDb datasets are {age:.1f} days old — refreshing.")

    note("Fetching IMDb datasets (once; then only the slim derivatives are kept)…")
    raw_basics = os.path.join(paths["raw"], "title.basics.tsv.gz")
    raw_ratings = os.path.join(paths["raw"], "title.ratings.tsv.gz")
    if not os.path.exists(raw_basics) or refresh:
        _download("title.basics.tsv.gz", raw_basics)
    if not os.path.exists(raw_ratings) or refresh:
        _download("title.ratings.tsv.gz", raw_ratings)

    _reduce_basics(raw_basics, paths["basics"])
    shutil.copyfile(raw_ratings, paths["ratings"])  # already small enough to keep whole
    shutil.rmtree(paths["raw"], ignore_errors=True)  # the raw quarter-gigabyte is disposable
    return paths


def _rows(path):
    """Stream a TSV.gz as dicts."""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        header = f.readline().rstrip("\n").split("\t")
        for line in f:
            values = line.rstrip("\n").split("\t")
            if len(values) == len(header):
                yield dict(zip(header, values))


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── scoring ──────────────────────────────────────────────────────────────────

def _type_ok(nf_type, imdb_type):
    if nf_type == "movie":
        return imdb_type in MOVIE_TYPES
    if nf_type == "show":
        return imdb_type in SHOW_TYPES
    return True


def score(rec, cand, key_weights):
    """How well an IMDb candidate explains a Netflix record. Higher is better; see the
    module docstring on why this is a transparent ladder rather than a fuzzy matcher."""
    best_title = 0.0
    for field in ("primaryTitle", "originalTitle"):
        weight = key_weights.get(norm(cand.get(field)))
        if weight:
            best_title = max(best_title, weight)
    if not best_title:
        return None
    total = 50 * best_title

    nf_year, start, end = rec.get("year"), _int(cand.get("startYear")), _int(cand.get("endYear"))
    if nf_year and start:
        if rec.get("type") == "show":
            # A Netflix show's releaseYear is usually its *newest* season — "The Handmaid's
            # Tale" reads 2025 against an IMDb startYear of 2017 — so a year inside the run
            # counts as a hit, not a miss.
            last = end or max(start, time.localtime().tm_year)
            if start <= nf_year <= last:
                total += 22
            elif abs(nf_year - start) <= 2:
                total += 12
            else:
                total -= 15 if abs(nf_year - start) <= 8 else 30
        else:
            # A film's year is the one thing Netflix and IMDb agree on, so a big gap almost
            # always means a *different film of the same name* — "Rich in Love" (2020,
            # Brazilian) matching a 1992 film, "Black Barbie" (2024) matching a 2016 short.
            # The penalty has to be steep enough to reject those outright rather than attach
            # them as a weak match: rejected titles then go to the suggestion endpoint, which
            # knows which one the name means.
            delta = abs(nf_year - start)
            total += (25 if delta == 0 else 18 if delta <= 1 else 10 if delta <= 2
                      else -15 if delta <= 5 else -32)
    elif nf_year and not start:
        total -= 5

    total += 15 if _type_ok(rec.get("type"), cand.get("titleType")) else -40

    runtime = _int(cand.get("runtimeMinutes"))
    if rec.get("type") == "movie" and runtime and 60 <= runtime <= 240:
        total += 3
    return total


# What counts as good enough to attach. The suggestion endpoint gets a lower bar than a blind
# dataset scan, because it has already decided the title answers the query.
ACCEPT_BASICS = 50
ACCEPT_SUGGEST = 45


def confidence(value):
    """The label on a *chosen* candidate. "none" is reserved for having chosen nothing at all:
    a row reading `source: suggestion, confidence: none` would claim both at once."""
    if value >= 85:
        return "exact"
    if value >= 70:
        return "strong"
    if value >= ACCEPT_BASICS:
        return "weak"
    return "guess"


# ── the suggestion endpoint (residue only) ───────────────────────────────────

def _suggest_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def suggest(title, cache, fetch=None):
    """Ask IMDb's search-box endpoint about a title. Cached forever — it's a free service."""
    key = norm(title)
    if key in cache:
        return cache[key]
    quoted = urllib.parse.quote(str(title).casefold().replace(" ", "%20"), safe="%")
    url = SUGGEST_URL.format(quoted)
    try:
        if fetch:
            data = fetch(url)
        else:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
    except Exception:  # noqa: BLE001 - a missed suggestion must never fail the join
        data = {}
    hits = []
    for entry in (data or {}).get("d", []) or []:
        if not str(entry.get("id", "")).startswith("tt"):
            continue
        hits.append({
            "tconst": entry["id"], "title": entry.get("l"),
            "year": entry.get("y"), "titleType": entry.get("qid") or entry.get("q"),
            "rank": entry.get("rank"),
        })
    cache[key] = hits
    return hits


# ── the join ─────────────────────────────────────────────────────────────────

def enrich(records, refresh=False, use_suggest=None, fetch=None):
    """Attach IMDb fields to every record, in place. Returns a small stats dict."""
    if not records:
        return {"matched": 0, "total": 0}
    paths = ensure(refresh=refresh)
    use_suggest = settings.IMDB_SUGGEST if use_suggest is None else use_suggest

    # Every key any record could match, pointing back at the records that want it.
    wanted = {}
    per_record = []
    for rec in records:
        key_weights = keys(rec.get("title"))
        per_record.append(key_weights)
        for key in key_weights:
            wanted.setdefault(key, []).append(rec)

    note(f"Matching {len(records):,} titles against IMDb ({len(wanted):,} title keys)…")
    candidates = {id(rec): [] for rec in records}
    scanned = 0
    for row in _rows(paths["basics"]):
        scanned += 1
        for field in ("primaryTitle", "originalTitle"):
            hits = wanted.get(norm(row.get(field)))
            if hits:
                for rec in hits:
                    candidates[id(rec)].append(row)
                break
    note(f"  scanned {scanned:,} IMDb titles")

    chosen = {}
    residue = []
    for rec, key_weights in zip(records, per_record):
        best, best_score, runner = None, None, None
        for cand in candidates[id(rec)]:
            value = score(rec, cand, key_weights)
            if value is None:
                continue
            if best_score is None or value > best_score:
                best, best_score, runner = cand, value, best
        if best and best_score >= ACCEPT_BASICS:
            chosen[id(rec)] = (best, best_score, "basics", runner)
        else:
            residue.append(rec)

    note(f"  {len(records) - len(residue):,} matched locally, {len(residue):,} to look up")

    # The residue goes to IMDb's own search, which knows alternate titles we don't.
    if use_suggest and residue:
        cache = _suggest_cache(paths["suggest"])
        before = len(cache)
        delay = max(0, settings.IMDB_SUGGEST_DELAY_MS) / 1000
        looked_up = 0
        for rec in residue:
            if looked_up >= settings.IMDB_SUGGEST_MAX:
                note(f"  suggestion cap ({settings.IMDB_SUGGEST_MAX}) reached — "
                     f"{len(residue) - looked_up:,} titles left unmatched this run")
                break
            cached = norm(rec.get("title")) in cache
            hits = suggest(rec.get("title"), cache, fetch=fetch)
            if not cached:
                looked_up += 1
                if delay:
                    time.sleep(delay)
            key_weights = keys(rec.get("title"))
            best, best_score = None, None
            for hit in hits:
                cand = {"tconst": hit["tconst"], "titleType": hit.get("titleType") or "",
                        "primaryTitle": hit.get("title"), "originalTitle": hit.get("title"),
                        "startYear": hit.get("year"), "endYear": "", "runtimeMinutes": "",
                        "genres": ""}
                value = score(rec, cand, key_weights)
                # The endpoint already decided this answers the query, so a title-key miss
                # is not disqualifying the way it is for a blind dataset scan.
                if value is None:
                    value = 34 + (15 if _type_ok(rec.get("type"), cand["titleType"]) else -40)
                    if rec.get("year") and _int(cand["startYear"]) == rec["year"]:
                        value += 25
                if best_score is None or value > best_score:
                    best, best_score = cand, value
            if best and best_score >= ACCEPT_SUGGEST:
                chosen[id(rec)] = (best, best_score, "suggestion", None)
        if len(cache) != before:
            with open(paths["suggest"], "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)

    # Ratings last, so suggestion hits are covered too — and only for the ids we kept.
    need = {info[0]["tconst"] for info in chosen.values()}
    ratings = {}
    for row in _rows(paths["ratings"]):
        if row["tconst"] in need:
            ratings[row["tconst"]] = (float(row["averageRating"]), int(row["numVotes"]))

    minimum, prior = settings.imdb_bayes()
    matched = 0
    for rec in records:
        info = chosen.get(id(rec))
        if not info:
            rec.update({
                "imdb_id": None, "imdb_rating": None, "imdb_votes": None, "weighted": None,
                "imdb_title": None, "imdb_year": None, "imdb_type": None,
                "imdb_runtime": None, "imdb_genres": None, "imdb_url": None,
                "match_source": "none", "match_confidence": "none", "match_score": None,
                "match_note": "no IMDb title matched",
            })
            continue
        cand, value, source, runner = info
        rating, votes = ratings.get(cand["tconst"], (None, None))
        weighted = None
        if rating is not None and votes:
            weighted = round((votes / (votes + minimum)) * rating
                             + (minimum / (votes + minimum)) * prior, 2)
        genres = [g for g in (cand.get("genres") or "").split(",") if g and g != r"\N"]
        note_text = f"{source}: {cand.get('primaryTitle')!r} ({cand.get('startYear') or '?'}, " \
                    f"{cand.get('titleType')})"
        if runner is not None:
            note_text += f"; runner-up {runner.get('tconst')} {runner.get('primaryTitle')!r}"
        if rating is None:
            note_text += "; IMDb has no rating for it"
        rec.update({
            "imdb_id": cand["tconst"],
            "imdb_rating": rating,
            "imdb_votes": votes,
            "weighted": weighted,
            "imdb_title": cand.get("primaryTitle"),
            "imdb_year": _int(cand.get("startYear")),
            "imdb_type": cand.get("titleType"),
            "imdb_runtime": _int(cand.get("runtimeMinutes")),
            "imdb_genres": genres,
            "imdb_url": f"https://www.imdb.com/title/{cand['tconst']}/",
            "match_source": source,
            "match_confidence": confidence(value),
            "match_score": round(value, 1),
            "match_note": note_text,
        })
        # runtime_min is IMDb's — Netflix's falcor endpoint refuses to hand over a runtime.
        rec.setdefault("runtime_min", None)
        if rec.get("runtime_min") is None:
            rec["runtime_min"] = _int(cand.get("runtimeMinutes"))
        if rating is not None:
            matched += 1

    rated = [r["imdb_rating"] for r in records if r.get("imdb_rating")]
    return {
        "total": len(records),
        "matched": matched,
        "unmatched": len(records) - matched,
        "median": round(sorted(rated)[len(rated) // 2], 1) if rated else None,
        "great": sum(1 for r in rated if r >= 8.0),
    }


def votes_log(votes):
    """Only used for display: a log scale keeps a 2 M-vote blockbuster from flattening a bar."""
    return round(math.log10(votes), 2) if votes else 0
