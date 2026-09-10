"""The subtitle library's index — what the Chrome extension reads to find an episode.

`gigaku subs` / `gigaku srt` write their SRT pairs into ``SRT_TARGET_DIR`` (by default the
``subs/`` folder inside chrome/gigaku, i.e. *inside* the extension), and this module keeps
an ``index.json`` beside them. An extension can fetch its own files but cannot list a
directory, so without an index it could only guess paths — and a guess needs the show's title
to match Netflix's exactly.

The index closes that gap from both ends:

* ``byId`` maps a **Netflix video id** — the number in ``/watch/81237996``, which `gigaku subs`
  already knows for every episode it rips — to a library entry. That match is exact and cares
  nothing for how a title is spelled or localised.
* ``byTitle`` is keyed by ``title|season|episode`` for everything else (files converted by
  `gigaku srt`, which never sees a Netflix id).

Both point at the same entries, so the two can't drift. Pure stdlib and pure logic: no browser,
no config import, so the tests exercise it directly.
"""
import json
import os
import re
import tempfile

from lib.subs.naming import sanitize

INDEX_NAME = "index.json"

# "<base> - Primary.srt" / "… - Secondary.srt" — the pair naming.srt_base() feeds.
_SRT = re.compile(r"^(?P<base>.+) - (?P<kind>Primary|Secondary)\.srt$", re.IGNORECASE)
# "<Title> - S01E03" — a base without it is a film (one export, no numbering).
_EPISODE = re.compile(r"^(?P<title>.+) - S(?P<season>\d{2})E(?P<episode>\d{2})$")


def parse_base(base: str) -> tuple[str, int | None, int]:
    """``'Dark - S02E03'`` → ``('Dark', 2, 3)``; a film's bare title → ``(title, None, 1)``."""
    m = _EPISODE.match(base)
    if not m:
        return base, None, 1
    return m["title"], int(m["season"]), int(m["episode"])


def title_key(title: str, season: int | None, episode: int) -> str:
    """The ``byTitle`` key. Sanitized and lower-cased so a title matches however it's cased."""
    return f"{sanitize(title).lower()}|{season or 0}|{episode}"


def scan(subs_dir: str) -> dict[str, dict]:
    """``byTitle`` for every complete Primary+Secondary pair under ``subs_dir/<Title>/``."""
    entries: dict[str, dict] = {}
    try:
        show_dirs = sorted(os.scandir(subs_dir), key=lambda e: e.name)
    except OSError:
        return entries

    for show in show_dirs:
        if not show.is_dir():
            continue
        pairs: dict[str, dict[str, str]] = {}
        for name in sorted(os.listdir(show.path)):
            m = _SRT.match(name)
            if m:
                pairs.setdefault(m["base"], {})[m["kind"].lower()] = name
        for base, files in pairs.items():
            if "primary" not in files or "secondary" not in files:
                continue  # a lone track can't be loaded as a pair — skip it silently
            title, season, episode = parse_base(base)
            entries[title_key(title, season, episode)] = {
                "dir": show.name,
                "primary": files["primary"],
                "secondary": files["secondary"],
                "title": title,
                "season": season,
                "episode": episode,
            }
    return entries


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update(subs_dir: str, ids: dict | None = None) -> str:
    """Rewrite ``subs_dir/index.json`` from what's on disk; return its path.

    ``ids`` maps a Netflix video id to ``(title, season, episode)`` — the ids learned during
    this run. Ids recorded by earlier runs are kept as long as their episode is still in the
    library, so re-indexing never loses them. The write is atomic: the extension may be
    fetching the index at any moment, and a half-written one would parse as "nothing here".
    """
    by_title = scan(subs_dir)
    path = os.path.join(subs_dir, INDEX_NAME)

    by_id = {k: v for k, v in _read(path).get("byId", {}).items() if v in by_title}
    for video_id, (title, season, episode) in (ids or {}).items():
        key = title_key(title, season, episode)
        if key in by_title:
            by_id[str(video_id)] = key

    os.makedirs(subs_dir, exist_ok=True)
    payload = json.dumps(
        {"byId": dict(sorted(by_id.items())), "byTitle": dict(sorted(by_title.items()))},
        ensure_ascii=False,
        indent=2,
    )
    fd, tmp = tempfile.mkstemp(dir=subs_dir, prefix=".index-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return path
