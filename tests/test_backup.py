"""`gigaku backup` — the pure logic: the shrink gate, the Migaku writer, the staleness
check, the alert, and `_swap`'s promise to touch only what it owns. No git, Chrome, or
Anki here — and no network: Telegram is captured, never called."""
import csv
import datetime as dt
import json

import pytest

from lib.vocab import backup, migaku
from lib.config import UserError


def _row(dict_form, secondary="", language="ja", known="KNOWN"):
    """A stand-in for one sqlite3.Row over migaku.DUMP_COLUMNS (dicts index the same way)."""
    return {
        "dictForm": dict_form,
        "secondary": secondary,
        "partOfSpeech": "n",
        "language": language,
        "knownStatus": known,
        "hasCard": 1,
        "tracked": 1,
        "del": 0,
        "created": 1_700_000_000_000,
        "mod": 1_700_000_000_000,
    }


# ── the shrink gate ──────────────────────────────────────────────────────────


def test_gate_passes_when_nothing_shrank():
    problems = backup._gate({"words": 100}, {"words": 100}, 10, force=False)
    assert problems == []


def test_gate_passes_when_data_grew():
    assert backup._gate({"words": 200}, {"words": 100}, 10, force=False) == []


def test_gate_allows_a_small_dip_within_the_threshold():
    # 95 of 100 is a 5% drop — noise, not a catastrophe.
    assert backup._gate({"words": 95}, {"words": 100}, 10, force=False) == []


def test_gate_refuses_a_drop_past_the_threshold():
    with pytest.raises(UserError, match="shrank"):
        backup._gate({"words": 80}, {"words": 100}, 10, force=False)


def test_gate_always_refuses_an_empty_dataset_even_on_the_first_run():
    # No previous manifest to compare against, but zero is never legitimate.
    with pytest.raises(UserError, match="empty"):
        backup._gate({"migaku": 0}, {}, 10, force=False)


def test_force_overrides_the_gate():
    assert backup._gate({"words": 0}, {"words": 100}, 10, force=True) == ["words is empty"]


def test_gate_names_every_offending_dataset():
    with pytest.raises(UserError) as exc:
        backup._gate({"words": 10, "migaku": 0}, {"words": 100, "migaku": 44000}, 10, False)
    message = str(exc.value)
    assert "words" in message and "migaku" in message


# ── the Migaku CSV writer ────────────────────────────────────────────────────


def test_migaku_writer_splits_by_language_and_orders_columns(tmp_path):
    rows = [_row("犬", language="ja"), _row("Hund", language="de")]
    total = backup._write_migaku(str(tmp_path), rows)

    assert total == 2
    ja = tmp_path / "migaku" / "ja.csv"
    de = tmp_path / "migaku" / "de.csv"
    assert ja.exists() and de.exists()

    header = next(csv.reader(ja.open(encoding="utf-8")))
    assert tuple(header) == migaku.DUMP_COLUMNS  # a stable, documented column order


def test_migaku_writer_sorts_rows_deterministically(tmp_path):
    # Same dictForm, different secondary → secondary breaks the tie; input order must not leak.
    rows = [_row("同", secondary="z"), _row("同", secondary="a"), _row("あ", secondary="")]
    backup._write_migaku(str(tmp_path), rows)

    data = list(csv.reader((tmp_path / "migaku" / "ja.csv").open(encoding="utf-8")))[1:]
    forms = [(r[0], r[1]) for r in data]
    assert forms == [("あ", ""), ("同", "a"), ("同", "z")]


# ── the commit message ───────────────────────────────────────────────────────


def test_commit_message_carries_the_date_and_the_headline_counts():
    message = backup._commit_message(
        "2026-07-23", {"words": 28621, "migaku": 44046, "ankimorphs_lemmas": 3123}
    )
    assert message == "2026-07-23 · 28,621 words · migaku 44,046 · ankimorphs 3,123"


# ── _swap ────────────────────────────────────────────────────────────────────


def test_swap_replaces_managed_paths_and_leaves_the_rest_alone(tmp_path):
    repo = tmp_path / "repo"
    staging = tmp_path / "staging"
    repo.mkdir()
    staging.mkdir()

    (repo / "README.md").write_text("hand-written, not ours to touch", encoding="utf-8")
    (repo / "words.json").write_text("[old]", encoding="utf-8")
    (repo / "migaku").mkdir()
    (repo / "migaku" / "stale.csv").write_text("gone next run", encoding="utf-8")

    (staging / "words.json").write_text("[new]", encoding="utf-8")
    (staging / "migaku").mkdir()
    (staging / "migaku" / "ja.csv").write_text("fresh", encoding="utf-8")

    backup._swap(str(staging), str(repo))

    assert (repo / "README.md").read_text(encoding="utf-8") == "hand-written, not ours to touch"
    assert (repo / "words.json").read_text(encoding="utf-8") == "[new]"
    assert (repo / "migaku" / "ja.csv").exists()
    assert not (repo / "migaku" / "stale.csv").exists()  # the whole dir was swapped, not merged


def test_swap_creates_managed_paths_that_did_not_exist_yet(tmp_path):
    repo = tmp_path / "repo"
    staging = tmp_path / "staging"
    repo.mkdir()
    staging.mkdir()
    (staging / "words.json").write_text("[first]", encoding="utf-8")

    backup._swap(str(staging), str(repo))
    assert (repo / "words.json").read_text(encoding="utf-8") == "[first]"


# ── not-a-repo guard ─────────────────────────────────────────────────────────


def test_require_repo_explains_the_one_time_setup(tmp_path):
    with pytest.raises(UserError, match="git repo"):
        backup._require_repo(str(tmp_path))  # a plain dir, no .git


# ── staleness ────────────────────────────────────────────────────────────────


def _ms(today, days_old):
    day = dt.date.fromordinal(today.toordinal() - days_old)
    return int(dt.datetime(day.year, day.month, day.day).timestamp() * 1000)


def _source(days_old, today, kind="site", contact_days=None):
    """A weighed candidate. `contact_days` is the second clock — how long since this copy
    of Migaku last reached its servers; None is an older bundle that has no such clock."""
    return {"kind": kind, "profile": "Profile 1", "max_mod": _ms(today, days_old),
            "sync_version": 1,
            "last_contact": None if contact_days is None else _ms(today, contact_days)}


def test_fresh_data_is_not_stale():
    today = dt.date(2026, 8, 1)
    assert backup._staleness(_source(0, today), today, 5) is None
    assert backup._staleness(_source(4, today), today, 5) is None


def test_data_frozen_past_the_threshold_names_the_freeze():
    today = dt.date(2026, 8, 1)
    reason = backup._staleness(_source(9, today, kind="extension"), today, 5)
    assert "9 days" in reason and "extension" in reason


def test_a_quiet_week_under_a_live_sync_is_silence():
    """2026-08-19, the alert's first firing and a false one: five days without a word
    marked, while the extension had re-minted its token that morning. Nothing was lost, and
    an alarm that goes off every holiday is one you learn to ignore."""
    today = dt.date(2026, 8, 19)
    assert backup._staleness(_source(5, today, kind="extension", contact_days=0), today, 5) is None
    assert backup._staleness(_source(30, today, contact_days=1), today, 5) is None
    # right up to the threshold: contact yesterday-but-four is still contact
    assert backup._staleness(_source(9, today, contact_days=4), today, 5) is None


def test_words_frozen_with_a_silent_sync_names_the_sync():
    """The failure that is worth a message: nothing has reached Migaku in days, so words
    marked on the phone are landing in neither the cache nor the backup."""
    today = dt.date(2026, 8, 19)
    reason = backup._staleness(_source(9, today, kind="extension", contact_days=6), today, 5)
    assert "9 days" in reason and "6 days" in reason
    assert "signed in" in reason  # names the sync, not "you stopped studying"


def test_a_bundle_with_no_contact_clock_is_judged_on_the_data_alone():
    """A witness we cannot hear is not an alibi — an older Migaku with no token or
    heartbeat to read must not silently switch the alarm off."""
    today = dt.date(2026, 8, 19)
    reason = backup._staleness(_source(9, today, contact_days=None), today, 5)
    assert "9 days" in reason


def test_a_missing_source_cannot_be_judged_stale():
    today = dt.date(2026, 8, 1)
    assert backup._staleness(None, today, 5) is None
    assert backup._staleness({"max_mod": 0}, today, 5) is None


def test_a_frozen_known_morphs_csv_reads_stale(tmp_path):
    """save_known_morphs rewrites each CSV every run, so mtime is today even on a quiet
    week — only its keep-the-existing-file refusals (broken venv, refused shrink) freeze
    it, and those noted to a log nobody reads while every backup said ok."""
    import os
    import time

    today = dt.date(2026, 8, 10)
    fresh = tmp_path / "gigaku_known_morphs_ja.csv"
    fresh.write_text("Morph-Lemma\nx\n", encoding="utf-8")
    frozen = tmp_path / "gigaku_known_morphs_de.csv"
    frozen.write_text("Morph-Lemma\ny\n", encoding="utf-8")
    old = time.mktime(dt.datetime(2026, 8, 1).timetuple())
    os.utime(frozen, (old, old))
    new = time.mktime(dt.datetime(2026, 8, 10).timetuple())
    os.utime(fresh, (new, new))

    reasons = backup._csv_staleness(["ja", "de", "fr"], str(tmp_path), today, 5)

    assert len(reasons) == 1  # fr has no file: not yet studied, not frozen
    assert "de" in reasons[0] and "9 days" in reasons[0]


# ── the alert ────────────────────────────────────────────────────────────────


def test_a_refused_gate_and_a_crash_get_different_status_words():
    # "refused" (data shrank, previous commit stands) reads very differently from a crash.
    assert backup._failure_status("Backup refused — data shrank unexpectedly: …") == "refused"
    assert backup._failure_status("Committed locally, but pushing to origin failed: …") == "failed"
    assert backup._failure_status("No word cache to back up: …") == "failed"


def test_headline_keeps_only_the_three_reported_numbers():
    headline = backup._headline(
        {"words": 28621, "migaku": 44046, "ankimorphs_lemmas": 3125,
         "known-morphs/removed-garbage.csv": 1528}
    )
    assert headline == {"words": 28621, "migaku": 44046, "ankimorphs_lemmas": 3125}


@pytest.fixture
def tg(monkeypatch):
    """Captured Telegram sends; credentials always 'exist'."""
    sent = []
    monkeypatch.setattr(backup.telegram, "credentials", lambda: ("token", "chat"))

    def fake_call(token, method, fields, files=None):
        sent.append((method, fields))
        return {"ok": True}

    monkeypatch.setattr(backup.telegram, "tg_call", fake_call)
    return sent


def test_a_good_run_alerts_nobody(tg):
    backup._alert({"name": "words", "status": "ok", "committed": True, "pushed": True})
    assert tg == []


def test_a_bad_run_names_the_reason_and_the_fate_of_the_commit(tg):
    backup._alert({
        "name": "words", "status": "failed",
        "reason": "Committed locally, but pushing to origin failed: timeout",
        "committed": True, "pushed": False,
        "counts": {"words": 28621, "migaku": 44046, "ankimorphs_lemmas": 3125},
    })

    (method, fields), = tg
    assert method == "sendMessage"
    assert "failed" in fields["text"]
    assert "pushing to origin failed" in fields["text"]
    assert "committed, not pushed" in fields["text"]
    assert "words 28,621" in fields["text"]


def test_a_stale_run_alerts_even_though_it_committed(tg):
    backup._alert({"name": "words", "status": "stale",
                   "reason": "Migaku's data (site origin) last changed 2026-07-22 — 9 days ago.",
                   "committed": True, "pushed": True})
    (_, fields), = tg
    assert "stale" in fields["text"] and "site origin" in fields["text"]


def test_a_crash_without_a_status_word_still_reads_as_failed(tg):
    backup._alert({"name": "words", "reason": "OSError: disk full"})
    (_, fields), = tg
    assert "failed" in fields["text"] and "disk full" in fields["text"]


def test_alert_failure_never_raises(monkeypatch):
    def boom():
        raise OSError("network is down")

    monkeypatch.setattr(backup.telegram, "credentials", boom)
    backup._alert({"name": "words", "status": "failed", "reason": "x"})  # swallow, not raise


# ── the crash path ───────────────────────────────────────────────────────────


def test_a_non_user_error_crash_still_alerts_then_reraises(monkeypatch):
    """The bug this pins: main() used to catch only UserError, so an OSError propagated
    with no alert anywhere — the one failure nobody would hear about."""
    alerts = []
    monkeypatch.setattr(backup, "_require_repo", lambda repo: None)
    monkeypatch.setattr(backup, "_ensure_gitattributes", lambda repo: None)
    monkeypatch.setattr(backup, "_ensure_gitignore", lambda repo: None)
    monkeypatch.setattr(backup, "_alert", alerts.append)
    monkeypatch.setattr(
        "lib.vocab.words.import_words",
        lambda: (_ for _ in ()).throw(OSError("disk went away")),
    )

    with pytest.raises(OSError):
        backup.main()

    (status,) = alerts
    assert status["status"] == "failed"
    assert status["reason"] == "OSError: disk went away"
    assert status["committed"] is False and status["pushed"] is False


# ── repo hygiene ─────────────────────────────────────────────────────────────


def test_gitignore_is_written_once_and_a_users_own_is_kept(tmp_path):
    backup._ensure_gitignore(str(tmp_path))
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".DS_Store\n"

    (tmp_path / ".gitignore").write_text("mine\n", encoding="utf-8")
    backup._ensure_gitignore(str(tmp_path))
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "mine\n"


def test_manifest_records_which_migaku_origin_the_snapshot_came_from(tmp_path):
    source = {"kind": "extension", "profile": "Profile 1", "max_mod": 5, "sync_version": 7,
              "last_contact": 9, "path": "/somewhere"}
    backup._write_manifest(str(tmp_path), {"words": 1}, "2026-08-01", source)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["migaku_source"] == {
        "kind": "extension", "profile": "Profile 1", "max_mod": 5, "sync_version": 7,
        "last_contact": 9,  # per-day, so "when did the sync actually die" has an answer
    }  # the path stays out — it's machine-local noise
