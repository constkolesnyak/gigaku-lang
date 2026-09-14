"""Read Migaku's word list — from Chrome's IndexedDB (no clicks) or from a CSV export.

Migaku Memory keeps its whole word list client-side, as a gzipped SQLite database in
IndexedDB — in TWO origins: the study.migaku.com site and the Migaku extension itself.
Either can hold the live copy and the other can rot: measured 2026-07, the site's blob
froze for nine days (44,046 rows, last change Jul 21) while the extension's kept syncing
(44,736 rows, current) — nine days of new words silently missing from cache and backup.
So `read_chrome()` reads *both* origins and picks the freshest by the data's own clock
(MAX(mod), then Migaku's sync cursor) — never by file size or by trusting one origin.

Each candidate also carries a *second* clock, `last_contact`: when that copy last reached
Migaku's servers, read off the auth token and the extension's heartbeat rather than off the
words. It decides nothing here — `pick_source` still picks purely on data freshness — and
exists for `backup._staleness`, which cannot otherwise tell a week with no studying from a
sync that died.

`read_exports()` still handles those CSVs (dictForm,secondary,hasCard,mod,language,
knownStatus) for anyone who prefers the manual route or has old exports lying around.

Ported from polyglotka (importer/migaku/{importer,browser}.py); pandas swapped for the csv
module, which is what reading a six-column CSV wants.
"""
import contextlib
import csv
import glob
import json
import os
import sqlite3
import tempfile
import zlib
from datetime import datetime

from lib.config import UserError, note, settings
from lib.vocab.words import Stage, Word

# Chrome names each origin's storage directory after the origin. Migaku's database exists
# under both of these; which one is current depends on which side synced last, so both are
# always candidates and freshness decides (see the module docstring).
MIGAKU_EXTENSION_ID = "dmeppfcidcpcocleneopiblmpnbokhep"
MIGAKU_ORIGINS = {
    "site": "https_study.migaku.com_0",
    "extension": f"chrome-extension_{MIGAKU_EXTENSION_ID}_0",
}

# Migaku's own vocabulary states, mapped onto ours. TRACKED is Migaku's "I've seen it and
# I'm on it"; IGNORED/UNKNOWN both become SKIPPED, which the merge in lib/vocab/words.py
# takes as "drop it from the store".
#
# **Those last two are not the same claim, and this mapping loses the difference.** IGNORED
# is "never show me this word again"; UNKNOWN is only "Migaku has met this word and you
# haven't marked it" — the default state of every word it has ever parsed. Measured on the
# Japanese list, 2026-08-01: **2,386 UNKNOWN against 7 IGNORED**. So `SKIPPED` from this
# source is, overwhelmingly, silence rather than judgement, and reading it as judgement is a
# mistake this comment exists to prevent (it was made, and it cost 190 words).
#
# The collapse is kept deliberately: it is what lets Migaku *un-learn* — a word you set back
# to UNKNOWN leaves the store instead of lingering at KNOWN forever, and there is no other
# signal for that. What it must not do is delete a word a *different* source currently calls
# known, and that is handled where the claims actually meet, in `words.merge`'s `keep`,
# rather than by teaching this table to lie in the other direction.
STAGES = {
    "KNOWN": Stage.KNOWN,
    "TRACKED": Stage.LEARNING,
    "LEARNING": Stage.LEARNING,
    "UNKNOWN": Stage.SKIPPED,
    "IGNORED": Stage.SKIPPED,
}


def _word(row):
    status = str(row["knownStatus"]).strip().upper()
    if status not in STAGES:
        return None  # a state Migaku added after this was written: not ours to guess at
    return Word(
        word=str(row["dictForm"]).strip(),
        language=str(row["language"]).strip().lower(),
        stage=STAGES[status],
        date=datetime.fromtimestamp(int(row["mod"]) / 1000),
    )


# ── CSV exports ──────────────────────────────────────────────────────────────


def read_exports(paths):
    words = []
    for path in paths:
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        found = [w for row in rows if (w := _word(row))]
        words += found
        note(f'Read {len(found):,} words from "{path}".')
    return words


# ── Chrome's IndexedDB ───────────────────────────────────────────────────────


def _chrome_dir():
    if settings.CHROME_DATA_DIR:
        path = os.path.expanduser(settings.CHROME_DATA_DIR)
        if not os.path.isdir(path):
            raise UserError(f"CHROME_DATA_DIR is not a directory: {path}")
        return path
    path = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if not os.path.isdir(path):
        raise UserError(f"Chrome's data directory is not where it should be: {path}")
    return path


def _size(path):
    """The file's size — or None for a directory, or for a blob already rotated away
    between the glob and the stat (see `_read`)."""
    try:
        return os.path.getsize(path) if os.path.isfile(path) else None
    except OSError:
        return None


def _blob_candidates(chrome_dir):
    """One candidate database per (origin, profile): the largest file in that origin's
    IndexedDB blob storage.

    Blob storage holds one file per stored Blob; Migaku stores its whole word database as a
    single gzipped one, so *within an origin* 'largest' picks it out reliably — but
    'largest across origins' would pick by file size what must be picked by freshness,
    which is `_pick()`'s job. Searching every profile (not just Default) is what makes
    this work on a Chrome with several signed-in profiles."""
    candidates = []
    for kind, origin in MIGAKU_ORIGINS.items():
        pattern = os.path.join(chrome_dir, "*", "IndexedDB", f"{origin}.indexeddb.blob")
        for blob_dir in glob.glob(pattern):
            blobs = [
                (size, f)
                for f in glob.glob(os.path.join(blob_dir, "**", "*"), recursive=True)
                if (size := _size(f)) is not None
            ]
            if blobs:
                candidates.append((kind, max(blobs)[1]))
    if not candidates:
        raise UserError(
            "No Migaku data in any Chrome profile. Log in at https://study.migaku.com in "
            "Chrome and open your word list once, or import a CSV export instead "
            "(gigaku import --no-chrome)."
        )
    return candidates


def _read(path):
    """The blob file's bytes, or None if Chrome rotated it away before we got there.

    Chrome never rewrites a blob in place: each write lands in a *new* numbered file and
    unlinks the old one. So a path found by `glob` is only a claim about the past, and the
    moment Migaku syncs it stops being true — which is how the 2026-08-05 backup died,
    having read the blob once at 00:05 and come back for it seconds later."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _inflate(data, path):
    """Chrome wraps the stored Blob in its own header, then gzip. Skip to the gzip magic and
    inflate what follows (raw deflate — the 10-byte gzip header is fixed-size)."""
    start = data.find(b"\x1f\x8b")
    if start < 0:
        raise UserError(f"Migaku's Chrome blob isn't gzipped as expected: {path}")
    try:
        return zlib.decompressobj(-zlib.MAX_WBITS).decompress(data[start + 10 :])
    except zlib.error as exc:
        raise UserError(f"Could not decompress Migaku's Chrome blob: {exc}")


@contextlib.contextmanager
def _db(sqlite_bytes):
    """Migaku's database as something sqlite3 can open, for the length of one read.

    Materialising ~30 MB is the price of asking a blob anything, so a caller with several
    questions (`_source_meta` asks five) opens the file once and asks them all against one
    connection rather than paying that write per query."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "migaku.db")
        with open(path, "wb") as f:
            f.write(sqlite_bytes)
        try:
            conn = sqlite3.connect(path)
        except sqlite3.Error as exc:
            raise UserError(f"Could not read Migaku's word database: {exc}")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise UserError(f"Could not read Migaku's word database: {exc}")


def _query(sqlite_bytes, sql):
    with _db(sqlite_bytes) as conn:
        return _rows(conn, sql)


def _local_value(conn, key):
    """One `localKeyValue` entry, JSON-decoded — None if the key, the table, or valid JSON
    is missing. Values are stored JSON-encoded, so a bare timestamp comes back an int and
    the token payload comes back a dict."""
    try:
        rows = _rows(conn, "SELECT entry FROM localKeyValue WHERE key = ?", (key,))
    except UserError:
        return None  # no such table — both origins carry it today, Migaku may not tomorrow
    if not rows:
        return None
    try:
        return json.loads(rows[0]["entry"])
    except (TypeError, ValueError):
        return None


def _number(value):
    """`value` as a number, or None — accepting the string form, because Migaku stores the
    heartbeat as a JSON number and the token's `iat` as a quoted string and there is no
    reason to think that will hold still."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _contact(conn):
    """When this copy last demonstrably reached Migaku's servers, in ms — or None.

    Three independent witnesses, newest wins; each may be absent from an older bundle or a
    renamed key, and a missing one is skipped rather than raised. Measured on the live
    extension blob 2026-08-19, five days into a gap with no word marked:

    * `localKeyValue['claims']` — the decoded Firebase ID token. `iat` is in **seconds**,
      unlike everything else here, and stored as a **string**, unlike everything else here
      (measured; an isinstance check that took only numbers dropped this witness entirely
      and the bug survived a green test suite, because the fixture was tidier than Migaku).
      The token lives an hour and only Migaku's servers can mint one, so a fresh `iat` is
      the strongest proof of life there is: minted 00:53 that morning, and the IndexedDB
      write at 00:53 says it is re-persisted on every refresh.
    * `localKeyValue['core.deviceMeta.lastSentAtMs']` — the extension reporting home,
      already in ms: 23:30 the evening before.
    * `MAX(mod)` over `learningMaterial` — Migaku pushing content *down*: six Korean course
      rows, a language nobody here studies, landed 2026-08-18 17:30. Proof a pull ran.

    Not one of them is evidence of loss, only of life, so this can only ever *silence*
    `backup._staleness` — never raise it. That asymmetry is what makes it safe to read
    clocks whose exact semantics are Migaku's to change."""
    stamps = []
    claims = _local_value(conn, "claims")
    iat = _number(claims.get("iat")) if isinstance(claims, dict) else None
    if iat:
        stamps.append(iat * 1000)
    sent = _number(_local_value(conn, "core.deviceMeta.lastSentAtMs"))
    if sent:
        stamps.append(sent)
    try:
        (row,) = _rows(conn, "SELECT MAX(mod) AS pulled FROM learningMaterial")
        if row["pulled"]:
            stamps.append(row["pulled"])
    except (UserError, ValueError):
        pass
    return int(max(stamps)) if stamps else None


def _source_meta(sqlite_bytes):
    """One candidate's freshness, on two clocks that answer two different questions.

    The **data** clock is MAX(mod) over every row (a deletion is activity too), plus
    Migaku's sync cursor when the LocalSync table exists — an older bundle may not have it,
    and that must degrade, not raise. It says whether words changed, and `pick_source`
    weighs candidates by it alone.

    The **contact** clock (`_contact`) says whether the sync that carries them is alive.
    Nothing but `backup._staleness` needs it, and it needs it badly: on the data clock
    alone a quiet week and a dead extension are the same reading."""
    with _db(sqlite_bytes) as conn:
        (row,) = _rows(conn, "SELECT MAX(mod) AS max_mod FROM WordList")
        meta = {"max_mod": row["max_mod"] or 0}
        try:
            (row,) = _rows(
                conn,
                "SELECT lastSyncedServerVersion AS v, lastPullSyncTimestamp AS t "
                "FROM LocalSync LIMIT 1",
            )
            meta.update(sync_version=row["v"], sync_time=row["t"])
        except (UserError, ValueError):
            meta.update(sync_version=None, sync_time=None)
        meta["last_contact"] = _contact(conn)
    return meta


def pick_source(sources):
    """The freshest candidate wins: newest MAX(mod) first, Migaku's sync version as the
    tiebreak. Pick, never merge — merging two snapshots of one database would resurrect
    rows the fresher one already deleted."""
    return max(sources, key=lambda s: (s["max_mod"], s["sync_version"] or -1))


# The chosen source, memoized per process: backup runs read_chrome() and dump_rows() back
# to back, and weighing every candidate twice would inflate ~30 MB for no new answer.
# Tests reset this to None when they repoint CHROME_DATA_DIR.
_SOURCE = None

# How many times to re-glob when a candidate blob is rotated out mid-read. Chrome's swap is
# a rename plus an unlink, so losing the same race three times running does not happen.
_PICK_ATTEMPTS = 3


def _weigh(chrome_dir):
    """Every readable candidate — bytes and all — plus whether one was rotated away.

    A vanished candidate is *not* skipped the way a corrupt one is. A blob disappears
    precisely because something wrote it, i.e. from the origin that is currently live, and
    dropping it would quietly hand the run to the stale origin — the exact silent failure
    this module was written to prevent. The caller re-globs instead."""
    sources, rotated = [], False
    for kind, path in _blob_candidates(chrome_dir):
        blob = _read(path)
        if blob is None:
            rotated = True
            continue
        try:
            meta = _source_meta(_inflate(blob, path))
        except UserError as exc:
            # The other origin may still be fine; a broken loser must not sink the run.
            note(f"Skipping unreadable Migaku blob ({kind}): {exc}")
            continue
        profile = os.path.relpath(path, chrome_dir).split(os.sep)[0]
        sources.append({"kind": kind, "path": path, "profile": profile, "blob": blob, **meta})
    return sources, rotated


def _pick():
    """The freshest readable candidate, with its bytes, memoized for the process.

    Keeping `blob` is the point: every later query answers from the copy taken here, so no
    path is ever opened twice and Chrome may rotate the file as freely as it likes. It also
    means read_chrome() and dump_rows() read one and the same snapshot — a backup whose
    words.json and migaku CSVs described two different minutes was always possible before,
    just quieter than the crash."""
    global _SOURCE
    if _SOURCE is None:
        chrome_dir = _chrome_dir()
        sources, rotated = _weigh(chrome_dir)
        for _ in range(_PICK_ATTEMPTS - 1):
            if not rotated:
                break
            note("Chrome rewrote a Migaku blob while it was being read — looking again.")
            sources, rotated = _weigh(chrome_dir)
        if rotated:
            note("A Migaku blob kept moving under the reader; picking from the origins "
                 "that stayed still.")
        if not sources:
            raise UserError(
                "Migaku's data in Chrome is unreadable in every origin. Open Migaku in "
                "Chrome so it rewrites its database, or import a CSV export instead "
                "(gigaku import --no-chrome)."
            )
        _SOURCE = pick_source(sources)
    return _SOURCE


def _describe(source):
    when = (
        datetime.fromtimestamp(source["max_mod"] / 1000).strftime("%Y-%m-%d")
        if source["max_mod"]
        else "empty"
    )
    sync = f", sync {source['sync_version']}" if source["sync_version"] is not None else ""
    # Named in every note because it is the one number that tells a holiday from a broken
    # extension, and re-deriving it by hand from the blob costs half an hour.
    contact = (
        f", contact {datetime.fromtimestamp(source['last_contact'] / 1000):%Y-%m-%d}"
        if source.get("last_contact") else ""
    )
    return f"{source['profile']}, {source['kind']} origin, last change {when}{sync}{contact}"


def _chrome_rows(sql):
    """Run `sql` against Migaku's word database, freshly pulled from Chrome's IndexedDB.

    The one place the blob→inflate→temp-SQLite dance lives; `read_chrome()` runs the
    store's four-column projection through it, `dump_rows()` the whole table for backup."""
    source = _pick()
    rows = _query(_inflate(source["blob"], source["path"]), sql)
    return rows, source


def read_chrome():
    rows, source = _chrome_rows(
        "SELECT dictForm, mod, language, knownStatus FROM WordList WHERE del = 0"
    )
    words = [w for row in rows if (w := _word(row))]
    note(f"Read {len(words):,} words from Migaku in Chrome ({_describe(source)}).")
    return words


# Columns worth snapshotting: everything but Migaku's server bookkeeping (serverMod,
# serverVersion), which churns every sync and means nothing off Migaku's servers. `del`
# stays — a deleted-but-not-purged row is still curation worth keeping.
DUMP_COLUMNS = (
    "dictForm", "secondary", "partOfSpeech", "language",
    "knownStatus", "hasCard", "tracked", "del", "created", "mod",
)


def dump_rows():
    """Migaku's *entire* WordList (all statuses, all languages) plus its source, for
    `gigaku backup`.

    Unlike read_chrome(), nothing is filtered or mapped: the UNKNOWN/IGNORED rows the store
    drops as SKIPPED are exactly the "never show me this again" curation that lives nowhere
    but here, so the backup keeps them verbatim. The source rides along so the manifest can
    record which origin the snapshot came from and how fresh it claimed to be."""
    columns = ", ".join(DUMP_COLUMNS)
    rows, source = _chrome_rows(f"SELECT {columns} FROM WordList")
    note(f"Read {len(rows):,} Migaku rows from Chrome ({_describe(source)}) for backup.")
    return rows, source
