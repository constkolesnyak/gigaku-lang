"""The Chrome side of the Migaku importer: two origins, freshest wins.

The bug these tests exist for: Migaku keeps its database in BOTH the study.migaku.com
origin and its extension's origin, and the one gigaku used to hardcode (the site) froze
for nine days while the extension kept syncing — a stale-but-readable source that no
"is it readable" check can catch. So the picker is pinned end to end: real SQLite bytes,
gzipped, wrapped in a junk prefix the way Chrome wraps blobs, laid out in both origins'
blob directories at different freshness.
"""
import gzip
import json
import sqlite3

import pytest

from lib.config import UserError, settings
from lib.vocab import migaku


def _db_bytes(tmp_path, name, rows, sync_version=None, sync_time=None,
              token_iat=None, sent_at=None, pulled=None, iat_as_string=True):
    """A gzipped Migaku word database the way it sits inside a Chrome blob file."""
    path = tmp_path / f"{name}.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE WordList (dictForm TEXT, secondary TEXT, partOfSpeech TEXT,"
        " language TEXT, knownStatus TEXT, hasCard INT, tracked INT, del INT,"
        " created INT, mod INT)"
    )
    conn.executemany(
        "INSERT INTO WordList VALUES (?, '', '', ?, ?, 0, 0, ?, ?, ?)",
        [(w, lang, status, dl, mod, mod) for (w, lang, status, dl, mod) in rows],
    )
    if sync_version is not None:
        conn.execute(
            "CREATE TABLE LocalSync (rowId INT, lastPullSyncTimestamp INT,"
            " lastSyncedServerVersion INT)"
        )
        conn.execute("INSERT INTO LocalSync VALUES (1, ?, ?)", (sync_time, sync_version))
    # The contact clock's three witnesses, each optional exactly as it is in a real bundle.
    # Migaku stores localKeyValue entries JSON-encoded, hence the json.dumps on a bare int.
    if token_iat is not None or sent_at is not None:
        conn.execute("CREATE TABLE localKeyValue (key TEXT, entry TEXT)")
        if token_iat is not None:
            # A string, as measured in the live bundle — the fixture must not be tidier
            # than Migaku, or the reader passes here and drops the witness in the field.
            stamp = str(token_iat) if iat_as_string else token_iat
            claims = {"user_id": "u", "iat": stamp, "exp": token_iat + 3600}
            conn.execute("INSERT INTO localKeyValue VALUES ('claims', ?)",
                         (json.dumps(claims),))
        if sent_at is not None:
            conn.execute(
                "INSERT INTO localKeyValue VALUES ('core.deviceMeta.lastSentAtMs', ?)",
                (json.dumps(sent_at),))
    if pulled is not None:
        conn.execute("CREATE TABLE learningMaterial (id TEXT, mod INT, lang TEXT)")
        conn.execute("INSERT INTO learningMaterial VALUES ('ko-course', ?, 'ko')", (pulled,))
    conn.commit()
    conn.close()
    # Chrome prepends its own blob header; _inflate finds the gzip magic past it.
    return b"chrome-blob-junk" + gzip.compress(path.read_bytes())


DAY = 86_400_000  # ms
OLD, NEW = 1_753_000_000_000, 1_753_000_000_000 + 9 * DAY


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    """A fake Chrome data dir wired into settings, with the picker's memo cleared."""
    monkeypatch.setattr(settings, "CHROME_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(migaku, "_SOURCE", None)

    def put(kind, blob, profile="Profile 1", name="420"):
        origin = migaku.MIGAKU_ORIGINS[kind]
        blob_dir = tmp_path / profile / "IndexedDB" / f"{origin}.indexeddb.blob" / "2" / "04"
        blob_dir.mkdir(parents=True, exist_ok=True)
        (blob_dir / name).write_bytes(blob)
        return blob_dir / name

    return tmp_path, put


# ── pick_source, pure ────────────────────────────────────────────────────────


def _src(mod, sync=None, kind="site"):
    return {"kind": kind, "path": kind, "profile": "p", "max_mod": mod,
            "sync_version": sync, "sync_time": None, "last_contact": None}


def test_newer_data_beats_older_regardless_of_origin():
    fresh = _src(NEW, sync=100, kind="extension")
    assert migaku.pick_source([_src(OLD, sync=999), fresh]) is fresh


def test_sync_version_breaks_a_mod_tie():
    ahead = _src(NEW, sync=20_536, kind="extension")
    assert migaku.pick_source([_src(NEW, sync=19_701), ahead]) is ahead


def test_a_missing_sync_cursor_loses_the_tie_but_still_competes():
    with_cursor = _src(NEW, sync=1)
    assert migaku.pick_source([_src(NEW, sync=None, kind="extension"), with_cursor]) is with_cursor
    assert migaku.pick_source([_src(NEW, sync=None)])["sync_version"] is None


# ── the whole dance, end to end ──────────────────────────────────────────────


def test_read_chrome_takes_the_fresher_origin_not_the_hardcoded_one(tmp_path, chrome):
    """The nine-day incident, in miniature: the site origin froze, the extension moved on."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)], sync_version=19_701))
    put("extension", _db_bytes(
        tmp_path, "ext",
        [("alt", "de", "KNOWN", 0, OLD), ("neu", "de", "KNOWN", 0, NEW)],
        sync_version=20_536,
    ))

    words = migaku.read_chrome()

    assert {w.word for w in words} == {"alt", "neu"}


def test_the_site_origin_still_wins_when_it_is_the_fresh_one(tmp_path, chrome):
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("neu", "de", "KNOWN", 0, NEW)]))
    put("extension", _db_bytes(tmp_path, "ext", [("alt", "de", "KNOWN", 0, OLD)]))

    assert {w.word for w in migaku.read_chrome()} == {"neu"}


def test_dump_rows_names_its_source_for_the_manifest(tmp_path, chrome):
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "UNKNOWN", 1, OLD)]))
    put("extension", _db_bytes(tmp_path, "ext", [("neu", "de", "KNOWN", 0, NEW)], sync_version=7))

    rows, source = migaku.dump_rows()

    assert source["kind"] == "extension" and source["sync_version"] == 7
    assert [r["dictForm"] for r in rows] == ["neu"]


def test_an_unreadable_loser_does_not_sink_the_run(tmp_path, chrome, capsys):
    """A blob that isn't the database (or is corrupt) is skipped with a note; the other
    origin still answers."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)]))
    put("extension", b"not a gzip blob at all")

    assert {w.word for w in migaku.read_chrome()} == {"alt"}
    assert "Skipping unreadable Migaku blob (extension)" in capsys.readouterr().err


def test_every_origin_unreadable_is_a_user_error(tmp_path, chrome):
    _, put = chrome
    put("site", b"garbage")
    put("extension", b"also garbage")

    with pytest.raises(UserError, match="unreadable in every origin"):
        migaku.read_chrome()


def test_no_blobs_at_all_says_log_in(chrome):
    with pytest.raises(UserError, match="Log in at https://study.migaku.com"):
        migaku.read_chrome()


def test_the_winner_is_memoized_per_process(tmp_path, chrome):
    """read_chrome() and dump_rows() in one run (backup does exactly this) must agree on
    the source without weighing every candidate twice."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("neu", "de", "KNOWN", 0, NEW)]))

    migaku.read_chrome()
    picked = migaku._SOURCE
    migaku.dump_rows()

    assert migaku._SOURCE is picked


# ── the contact clock ────────────────────────────────────────────────────────


def test_the_token_dates_the_last_contact(tmp_path, chrome):
    """The auth token is the strongest witness: an hour-lived thing only Migaku's servers
    can mint, and `iat` is in seconds where every other stamp here is in ms."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)],
                          token_iat=NEW // 1000))

    migaku.read_chrome()

    assert migaku._SOURCE["last_contact"] == NEW


def test_a_numeric_token_stamp_reads_the_same(tmp_path, chrome):
    """Migaku quotes `iat` today; nothing says it must tomorrow."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)],
                          token_iat=NEW // 1000, iat_as_string=False))

    migaku.read_chrome()

    assert migaku._SOURCE["last_contact"] == NEW


def test_the_newest_witness_wins(tmp_path, chrome):
    """Three clocks, and the question is only ever 'when did this copy last reach Migaku',
    so the answer is the latest of them — a stale heartbeat beside a fresh token means the
    extension is alive, not half-dead."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)],
                          token_iat=OLD // 1000, sent_at=NEW, pulled=OLD))

    migaku.read_chrome()

    assert migaku._SOURCE["last_contact"] == NEW


def test_content_pushed_down_counts_as_contact(tmp_path, chrome):
    """Measured 2026-08-18: six Korean course rows arrived in a bundle whose owner studies
    no Korean. Nobody marked them — a pull did, which is the thing being asked about."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)], pulled=NEW))

    migaku.read_chrome()

    assert migaku._SOURCE["last_contact"] == NEW


def test_a_bundle_with_no_witnesses_reports_no_contact(tmp_path, chrome):
    """None, not zero, and never an exception: backup._staleness reads None as 'cannot
    tell' and falls back to judging the data clock alone."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)]))

    migaku.read_chrome()

    assert migaku._SOURCE["last_contact"] is None


def test_the_contact_clock_never_decides_which_origin_wins(tmp_path, chrome):
    """Freshness of *data* picks the source; contact only ever explains it. An origin that
    phoned home yesterday with a month-old word list is still the loser."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)],
                          token_iat=NEW // 1000))
    put("extension", _db_bytes(tmp_path, "ext", [("neu", "de", "KNOWN", 0, NEW)]))

    assert {w.word for w in migaku.read_chrome()} == {"neu"}


def test_the_note_names_the_contact_date(tmp_path, chrome, capsys):
    """Half an hour of hand-decompressing a blob is what it costs to answer 'was it
    syncing?' after the fact; the note answers it for free."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)], sent_at=NEW))

    migaku.read_chrome()

    assert "contact 2025-07-29" in capsys.readouterr().err


# ── Chrome rotating a blob out from under the reader ─────────────────────────


def test_a_blob_that_vanishes_after_the_first_read_still_backs_up(tmp_path, chrome):
    """The 2026-08-05 crash: read_chrome() at 00:05, Migaku syncs, Chrome unlinks the blob
    it just read, and dump_rows() went back to a path that no longer existed. It must not
    go back at all — the bytes were already in hand."""
    _, put = chrome
    blob = put("site", _db_bytes(tmp_path, "site", [("neu", "de", "KNOWN", 0, NEW)]))

    assert {w.word for w in migaku.read_chrome()} == {"neu"}
    blob.unlink()  # Chrome rotates: new file, old one gone

    rows, _ = migaku.dump_rows()
    assert [r["dictForm"] for r in rows] == ["neu"]


def test_both_reads_answer_from_one_snapshot(tmp_path, chrome):
    """Not just survival — agreement. A blob rewritten between the two reads used to give
    a backup whose words.json and migaku CSVs described two different minutes."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("neu", "de", "KNOWN", 0, NEW)]))

    migaku.read_chrome()
    put("site", _db_bytes(tmp_path, "later", [("später", "de", "KNOWN", 0, NEW)]))

    rows, _ = migaku.dump_rows()
    assert [r["dictForm"] for r in rows] == ["neu"]


def test_a_rotating_blob_is_re_read_not_conceded_to_the_stale_origin(tmp_path, chrome,
                                                                     monkeypatch, capsys):
    """The live origin is the one being written, so it is the one that vanishes. Skipping
    it like a corrupt blob would silently crown the frozen origin — the nine-day incident
    all over again, this time caused by the fix for it."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)]))
    put("extension", _db_bytes(tmp_path, "ext", [("neu", "de", "KNOWN", 0, NEW)]))

    real_read, missed = migaku._read, []

    def rotate_once(path):
        if "chrome-extension" in path and not missed:
            missed.append(path)
            return None  # gone the first time we reach for it
        return real_read(path)

    monkeypatch.setattr(migaku, "_read", rotate_once)

    assert {w.word for w in migaku.read_chrome()} == {"neu"}
    assert "looking again" in capsys.readouterr().err


def test_a_blob_that_never_stops_moving_falls_back_to_what_stood_still(tmp_path, chrome,
                                                                       monkeypatch, capsys):
    """Losing the race every single attempt is not a reason to have no backup at all — but
    it is a reason to say so, since the origin that answered may be the stale one."""
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)]))
    put("extension", _db_bytes(tmp_path, "ext", [("neu", "de", "KNOWN", 0, NEW)]))

    real_read = migaku._read
    monkeypatch.setattr(
        migaku, "_read",
        lambda path: None if "chrome-extension" in path else real_read(path),
    )

    assert {w.word for w in migaku.read_chrome()} == {"alt"}
    assert "kept moving" in capsys.readouterr().err


def test_every_blob_moving_at_once_is_a_user_error(tmp_path, chrome, monkeypatch):
    _, put = chrome
    put("site", _db_bytes(tmp_path, "site", [("alt", "de", "KNOWN", 0, OLD)]))
    monkeypatch.setattr(migaku, "_read", lambda path: None)

    with pytest.raises(UserError, match="unreadable in every origin"):
        migaku.read_chrome()
