"""The ripped title list: the record shape, its flat projection, and the on-disk dump.

Two layers on purpose. ``raw`` keeps Netflix's ``itemSummary`` object verbatim, so a field
nobody thought to project is still there next month without a re-rip. ``flat()`` is the
narrow, stable view the CSV, the TSV and the HTML table all read — the only thing allowed to
know column names.

Pure stdlib and free of Chrome, so the tests exercise it directly.
"""
import csv
import json
import os
import tempfile

# The flat table's columns, in order. The CSV header *is* this list, and the HTML page reads
# the same keys — one definition, so a renamed column can't drift between them.
FLAT_COLUMNS = [
    "rank", "nf_id", "title", "type", "year", "runtime_min", "seasons", "episodes",
    "seasons_label", "is_original", "watched", "is_anime", "is_korean", "nf_genres", "maturity", "maturity_label", "maturity_board",
    "maturity_level", "in_my_list", "remind_me", "playable", "boxart_url", "nf_url",
    "imdb_id", "imdb_rating", "imdb_votes", "weighted", "imdb_title", "imdb_year",
    "imdb_type", "imdb_runtime", "imdb_genres", "imdb_url",
    "match_source", "match_confidence", "match_score", "match_note",
    # Empty when the browse gallery's own list ("su") holds the title; otherwise where it did
    # come from — a sort list the storefront doesn't show, or "manual" for TITLES_EXTRA_IDS.
    # See lib/netflix/gallery.py: the node is not the catalogue, so this is not decoration.
    "discovery",
]

# Netflix calls them "show" and "movie"; keep those words rather than inventing our own.
TYPE_SHOW, TYPE_MOVIE = "show", "movie"


def _dig(obj, *path, default=None):
    """obj["a"]["b"] without caring which level is missing or isn't a dict."""
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return default
        obj = obj[key]
    return obj if obj is not None else default


def _boxart_url(item):
    """Netflix's boxArt shape has changed more than once; take a URL wherever it is."""
    art = item.get("boxArt")
    if isinstance(art, str):
        return art
    if isinstance(art, dict):
        for key in ("url", "src", "value"):
            if isinstance(art.get(key), str):
                return art[key]
        for value in art.values():  # {"width": {"url": ...}} style nesting
            if isinstance(value, dict) and isinstance(value.get("url"), str):
                return value["url"]
    return None


def record(index, entry):
    """One gallery slot → our record, or None if the slot is past the end of the list.

    Falcor materialises indexes beyond the list as present-but-empty rather than erroring, so
    "no itemSummary" is exactly how the rip learns it has reached the end.
    """
    item = entry.get("itemSummary") if isinstance(entry, dict) else None
    if not isinstance(item, dict) or not item.get("title"):
        return None

    nf_id = item.get("videoId") or item.get("id")
    rating = _dig(item, "maturity", "rating", default={})
    return {
        "rank": index,
        "nf_id": nf_id,
        "title": item.get("title"),
        "type": item.get("type"),
        "year": item.get("releaseYear"),
        "seasons": item.get("seasonCount"),
        "episodes": item.get("episodeCount") or entry.get("episodeCount"),
        "seasons_label": item.get("numSeasonsLabel"),
        "is_original": bool(item.get("isOriginal")),
        # Filled by lib/netflix/watched.py from the Netflix Watched Marker extension; False
        # until then, so the column exists whether or not that sync ran.
        "watched": False,
        # Filled by lib/netflix/markers.py from Netflix's own per-title genre names.
        "is_anime": False,
        "is_korean": False,
        "nf_genres": [],
        "maturity": _dig(rating, "value"),
        "maturity_label": _dig(rating, "maturityDescription"),
        "maturity_board": _dig(rating, "board"),
        "maturity_level": _dig(rating, "maturityLevel"),
        "in_my_list": bool(_dig(entry, "queue", "inQueue", default=False)),
        "remind_me": bool(entry.get("inRemindMeList")),
        "playable": bool(_dig(item, "availability", "isPlayable", default=True)),
        "boxart_url": _boxart_url(item),
        "nf_url": f"https://www.netflix.com/title/{nf_id}" if nf_id else None,
        # Set by lib/netflix/gallery.py once it knows which of the node's lists found this —
        # "" means the storefront listed it, which is the only case that isn't news.
        "discovery": "",
        # Everything Netflix sent, untouched — the point of "maximum metadata".
        "raw": item,
    }


def flat(rec):
    """The flat row: every FLAT_COLUMNS key, missing ones as ''."""
    return {col: rec.get(col, "") if rec.get(col) is not None else "" for col in FLAT_COLUMNS}


def write_json(path, payload):
    """Atomic write — a crash or a Ctrl-C mid-rip must not shred a good dump."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".titles-",
                               suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None  # a truncated dump is a cache miss, not a crash


def write_csv(path, records, delimiter=","):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FLAT_COLUMNS, delimiter=delimiter,
                               extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = flat(rec)
            for key, value in row.items():
                if isinstance(value, list):
                    row[key] = "|".join(str(v) for v in value)
                elif isinstance(value, str):
                    row[key] = " ".join(value.split())  # no raw newlines in a CSV cell
            writer.writerow(row)
    return path


def tsv(records):
    """The flat table as a TSV string — `gigaku titles --tsv | pbcopy`."""
    lines = ["\t".join(FLAT_COLUMNS)]
    for rec in records:
        row = flat(rec)
        lines.append("\t".join(
            "|".join(str(v) for v in row[c]) if isinstance(row[c], list)
            else " ".join(str(row[c]).split())
            for c in FLAT_COLUMNS
        ))
    return "\n".join(lines)
