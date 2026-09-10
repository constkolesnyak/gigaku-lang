"""`gigaku backup` — a daily, versioned, off-site snapshot of the irreplaceable data.

Three datasets on this Mac are the *only* copy of years of study, and nothing else guards
them (the word cache is excluded from Time Machine; AnkiWeb syncs the collection but not the
add-on CSVs; Migaku's dropped-word curation lives nowhere but Chrome's IndexedDB):

* the word cache (`words.json`) — the sole per-day history behind the plots;
* Migaku's **entire** raw word list — every status, including the UNKNOWN/IGNORED rows the
  store discards as SKIPPED, i.e. the "never show me this word again" work;
* AnkiMorphs' hand-curated CSVs — AnkiWeb syncs the collection, never add-on data. (The
  prize exhibit here, `removed-garbage.csv`, was absorbed into the word cache and deleted
  2026-08-01 — it lives on in this repo's history, which is the point of a versioned
  backup.)

Each is written as **plain text** into BACKUP_DIR (a git repo) and pushed to BACKUP_REMOTE.
Git is the point: every day is one commit, so any past state restores with a single
`git checkout`, and the daily text diffs pack down to a few KB.

**Stage, then gate, then swap.** Everything is gathered into a temp dir first; only if the
counts pass the shrink gate is the repo touched. So a backup can never faithfully mirror a
catastrophe — a logged-out Chrome, a wiped language, a renamed Anki profile makes `_gather`
raise or `_gate` refuse, and the previous good commit simply stands. Without that, the first
bad day would quietly commit a hollow file over a good one.
"""
import csv
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

from lib import config, telegram
from lib.config import UserError, note, settings
from lib.vocab import migaku

# The repo entries this command owns and rewrites each run. Everything else in BACKUP_DIR
# (a README, .git) is left untouched — `_swap` only ever replaces these.
MANAGED = ("words.json", "migaku", "ankimorphs", "manifest.json")


def main(force=False):
    from lib.vocab.words import import_words

    repo = settings.BACKUP_DIR
    # Built up as we go so that whatever happens — success, a refused gate, a failed push,
    # a crash — the alert can say what was true at the moment it went wrong. A bad run
    # alerts in every exit path, then re-raises.
    status = {"name": "words", "started_at": _now_iso(), "via_host": socket.gethostname()}
    try:
        _require_repo(repo)
        _ensure_gitattributes(repo)
        _ensure_gitignore(repo)

        # Refresh from Chrome/LR first (non-consuming, exactly as publish does), so the
        # snapshot is today's data and the site we publish minutes later shows the same numbers.
        words = import_words()

        # AnkiMorphs must see today's Migaku knowledge in both languages on every daily
        # run (user's rule, 2026-08-06) — the consume-only path left the known-morphs
        # CSVs to manual `gigaku import`s. Best-effort per language: a missing spaCy
        # venv notes and keeps the existing de file, and must not sink the snapshot.
        from lib.vocab.words import save_known_morphs
        for lang in settings.save_langs():
            save_known_morphs(lang, words)

        date = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as staging:
            counts, migaku_source = _gather(staging, words, date)
            _gate(counts, _prev_counts(repo), settings.BACKUP_SHRINK_PCT, force)
            _swap(staging, repo)

        status["counts"] = _headline(counts)
        status["committed"] = _commit(repo, _commit_message(date, counts))
        status["pushed"] = _push(repo, settings.BACKUP_REMOTE) if status["committed"] else False
        # The snapshot is safe either way; staleness is a warning about the *source*, so it
        # comes after the commit — refusing would also stop backing up the two healthy
        # datasets, and a frozen origin is Chrome's problem, not the repo's.
        today = datetime.now().date()
        reasons = [_staleness(migaku_source, today, settings.BACKUP_STALE_DAYS)]
        reasons += _csv_staleness(settings.save_langs(), settings.KNOWN_MORPHS_DIR,
                                  today, settings.BACKUP_STALE_DAYS)
        stale = "\n".join(r for r in reasons if r)
        if stale:
            status.update(status="stale", reason=stale)
            note(f"Backup ran on stale data: {stale}")
        else:
            status.update(status="ok", reason="")
    except UserError as exc:
        # committed/pushed may already be set (a push that failed after a good commit); keep
        # those so the alert can say "committed locally, push failed" rather than guess.
        status.setdefault("committed", False)
        status.setdefault("pushed", False)
        status.update(status=_failure_status(str(exc)), reason=str(exc))
        _alert(status)
        raise
    except Exception as exc:
        # A crash that isn't a UserError (missing git binary, a renamed Migaku column, a
        # full disk) used to propagate with no alert at all — the one kind of failure
        # nobody would hear about until the data was needed.
        status.setdefault("committed", False)
        status.setdefault("pushed", False)
        status.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        _alert(status)
        raise
    _alert(status)


# ── gather ───────────────────────────────────────────────────────────────────


def _gather(staging, words, date):
    """Assemble the whole snapshot in `staging`; return the per-dataset counts and the
    Migaku source (which origin, how fresh — the staleness check runs on it after commit).

    Raises rather than returns a partial snapshot: a dataset that can't be read is not a
    dataset that should be silently dropped from the backup."""
    counts = {}

    if not os.path.exists(config.WORDS_CACHE):
        raise UserError(f"No word cache to back up: {config.WORDS_CACHE}")
    shutil.copyfile(config.WORDS_CACHE, os.path.join(staging, "words.json"))
    counts["words"] = len(words)

    migaku_rows, migaku_source = migaku.dump_rows()
    counts["migaku"] = _write_migaku(staging, migaku_rows)

    os.makedirs(os.path.join(staging, "ankimorphs"))
    db = os.path.join(settings.ANKI_PROFILE_DIR, "ankimorphs.db")
    counts["ankimorphs_lemmas"] = _dump_lemmas(staging, db)
    counts.update(_copy_csvs(staging, settings.ANKI_PROFILE_DIR, "known-morphs"))
    counts.update(_copy_csvs(staging, settings.ANKI_PROFILE_DIR, "priority-files"))

    _write_manifest(staging, counts, date, migaku_source)
    return counts, migaku_source


def _write_migaku(staging, rows):
    """One CSV per language, rows sorted — so a diff is readable and a day's delta is tiny.

    A single 44k-row file would rewrite entirely on any reordering; sorting by
    (dictForm, secondary) pins the order so git sees only the day's real changes."""
    mdir = os.path.join(staging, "migaku")
    os.makedirs(mdir)
    by_lang = {}
    for row in rows:
        by_lang.setdefault(row["language"], []).append(row)
    for lang, lang_rows in by_lang.items():
        lang_rows.sort(key=lambda r: (r["dictForm"], r["secondary"]))
        with open(os.path.join(mdir, f"{lang}.csv"), "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")  # LF, not csv's default CRLF
            writer.writerow(migaku.DUMP_COLUMNS)
            writer.writerows([row[c] for c in migaku.DUMP_COLUMNS] for row in lang_rows)
    return len(rows)


def _dump_lemmas(staging, db_path):
    """The AnkiMorphs known lemmas (interval ≥ 1), one per row, sorted.

    Hot-copied via the SQLite backup API rather than read in place: Anki may be running with
    a live WAL, and `.backup()` yields a consistent snapshot without ever risking a lock."""
    if not os.path.exists(db_path):
        raise UserError(f"AnkiMorphs database not found: {db_path}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "ankimorphs.db")
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(copy)
                src.backup(dst)
                dst.close()
            finally:
                src.close()
            conn = sqlite3.connect(copy)
            rows = conn.execute(
                "SELECT lemma, MAX(highest_lemma_learning_interval) "
                "FROM Morphs WHERE highest_lemma_learning_interval >= 1 "
                "GROUP BY lemma ORDER BY lemma"
            ).fetchall()
            conn.close()
    except sqlite3.Error as exc:
        raise UserError(f"Could not read the AnkiMorphs database: {exc}")

    with open(os.path.join(staging, "ankimorphs", "lemmas.csv"), "w",
              encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")  # LF, not csv's default CRLF
        writer.writerow(["lemma", "interval"])
        writer.writerows(rows)
    return len(rows)


def _copy_csvs(staging, profile_dir, sub):
    """Copy every CSV in profile_dir/<sub> verbatim; return {'<sub>/<name>': line_count}.

    An absent directory is a warning, not a failure — not everyone runs known-morphs — but
    the gate still guards whatever files are present (removed-garbage.csv is priceless)."""
    src = os.path.join(profile_dir, sub)
    if not os.path.isdir(src):
        note(f"No {sub}/ directory under {profile_dir} — skipping.")
        return {}
    out = os.path.join(staging, "ankimorphs", sub)
    os.makedirs(out)
    counts = {}
    for name in sorted(os.listdir(src)):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(src, name)
        shutil.copyfile(path, os.path.join(out, name))
        counts[f"{sub}/{name}"] = _line_count(path)
    return counts


def _line_count(path):
    with open(path, "rb") as f:
        return sum(1 for _ in f)


def _write_manifest(staging, counts, date, migaku_source):
    manifest = {
        "date": date,
        "counts": counts,
        "sources": {
            "words": config.WORDS_CACHE,
            "migaku": "Chrome IndexedDB, full WordList (see migaku_source)",
            "ankimorphs": settings.ANKI_PROFILE_DIR,
        },
        # Which origin won and how fresh it claimed to be — so a later "when did this go
        # wrong" question has a per-day answer in the repo's own history.
        "migaku_source": {
            k: migaku_source.get(k)
            for k in ("kind", "profile", "max_mod", "sync_version", "last_contact")
        },
    }
    with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


# ── gate ─────────────────────────────────────────────────────────────────────


def _prev_counts(repo):
    path = os.path.join(repo, "manifest.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("counts", {})
    except (json.JSONDecodeError, OSError):
        return {}  # a corrupt prior manifest shouldn't block today's backup


def _gate(counts, previous, shrink_pct, force):
    """Refuse a snapshot where any dataset is empty or shrank past the threshold.

    Empty always trips (a dataset we snapshot is never legitimately zero); a drop beyond
    shrink_pct versus the last manifest trips too. `force` waves it through for a real
    mass-delete. Returns the list of problems (for tests / the caller's log)."""
    problems = []
    for name, n in counts.items():
        if n == 0:
            problems.append(f"{name} is empty")
        elif name in previous and n < previous[name] * (1 - shrink_pct / 100):
            problems.append(f"{name} shrank {previous[name]:,} → {n:,}")
    if problems and not force:
        raise UserError(
            "Backup refused — data shrank unexpectedly (re-run with --force if this is real):"
            + "".join(f"\n  · {p}" for p in problems)
        )
    return problems


# ── commit ───────────────────────────────────────────────────────────────────


def _swap(staging, repo):
    """Replace the managed entries in the repo from staging; leave everything else alone."""
    for name in MANAGED:
        dst = os.path.join(repo, name)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.remove(dst)
        src = os.path.join(staging, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        elif os.path.exists(src):
            shutil.copyfile(src, dst)


def _commit_message(date, counts):
    return (
        f"{date} · {counts['words']:,} words · "
        f"migaku {counts['migaku']:,} · ankimorphs {counts['ankimorphs_lemmas']:,}"
    )


def _commit(repo, message):
    """Stage and commit; return True if a commit was made, False if nothing changed.

    A slept Mac that wakes to identical data won't stack empty commits — and the caller
    reports "nothing changed" rather than a phantom success."""
    _git(repo, "add", "-A")
    if not _has_staged_changes(repo):
        note("Nothing changed since the last backup — no commit.")
        return False
    _git(repo, "commit", "-m", message)
    note(f"Committed: {message}")
    return True


def _push(repo, remote):
    """Push HEAD; return True on success, False when there's no remote. Raise on failure.

    Called only after the local commit is safely made, so a GitHub outage never loses data —
    it just leaves an unpushed commit that the next run carries up."""
    if not remote:
        note("BACKUP_REMOTE is empty — committed locally, not pushed.")
        return False
    result = subprocess.run(
        ["git", "-C", repo, "push", remote, "HEAD"], capture_output=True, text=True
    )
    if result.returncode:
        raise UserError(
            f"Committed locally, but pushing to {remote} failed: "
            f"{result.stderr.strip() or 'git push failed'}"
        )
    note(f"Pushed to {remote}.")
    return True


# ── the alert ────────────────────────────────────────────────────────────────


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _headline(counts):
    """The three numbers the alert shows — the rest of `counts` is per-file detail."""
    return {k: counts.get(k) for k in ("words", "migaku", "ankimorphs_lemmas")}


def _failure_status(message):
    """Map a UserError to a status word: a refused gate reads very differently from a
    crash or a failed push, so it gets its own."""
    return "refused" if message.startswith("Backup refused") else "failed"


def _staleness(source, today, stale_days):
    """None, or why today's snapshot ran on frozen Migaku data.

    The shrink gate cannot see this: a frozen source produces the same counts day after
    day, which is exactly what a quiet week produces. Neither can the data's own
    last-change date, which is all this used to ask — and on **2026-08-19, its first
    firing ever, it was wrong**: five days with no word marked, while that same blob showed
    the extension re-minting its auth token at 00:53 that morning and pulling Migaku's
    Korean course content the evening before. Nothing was going anywhere; the alert said
    words were being lost.

    So it takes **two** clocks and fires only when both have stopped. `max_mod` says
    whether words changed; `last_contact` (`migaku._contact`) says whether the sync that
    would carry them is alive. Frozen words under a live sync is a holiday, and must be
    silence — an alert that goes off every time you take a week away is one you learn to
    swipe past, which costs you exactly the freeze it exists to catch. Frozen words *and*
    no contact for `stale_days` is the real thing — Chrome never opened, the extension
    disabled, Migaku signed out — and then anything marked on another device is reaching
    neither the cache nor the backup.

    A source with no contact clock at all (an older bundle, a key Migaku renamed) is judged
    on the data clock alone: a witness we cannot hear must not be read as an alibi. The
    alert repeats daily while the freeze lasts; it names an ongoing loss."""
    if not source or not source.get("max_mod"):
        return None
    last = datetime.fromtimestamp(source["max_mod"] / 1000).date()
    age = (today - last).days
    if age < stale_days:
        return None
    if not source.get("last_contact"):
        return (
            f"Migaku's data ({source.get('kind')} origin) last changed {last} — {age} days "
            f"ago, and this bundle has no sync clock to check. Words marked since then may "
            f"be reaching neither the cache nor the backup; open Migaku in Chrome so it syncs."
        )
    seen = datetime.fromtimestamp(source["last_contact"] / 1000).date()
    silent = (today - seen).days
    if silent < stale_days:
        return None  # words idle, sync alive: a break, and saying otherwise trains the alarm away
    return (
        f"Migaku's data ({source.get('kind')} origin) last changed {last} — {age} days "
        f"ago — and it last reached Migaku's servers on {seen}, {silent} days ago. The sync "
        f"itself is down, so words marked on any device are reaching neither the cache nor "
        f"the backup; open Chrome and check Migaku is still signed in."
    )


def _csv_staleness(langs, known_morphs_dir, today, stale_days):
    """Why a known-morphs CSV has been frozen — `save_known_morphs`' refusals made visible.

    Every healthy run rewrites each configured CSV, so its mtime is today even on a quiet
    week; only the keep-the-existing-file paths (a broken spaCy venv, a refused shrink)
    leave it behind. Those refusals note to stderr, which under launchd is a log nobody
    reads — the measured failure mode was the de CSV freezing for weeks while every backup
    reported ok. Past `stale_days` the alert channel says so, daily, exactly like the
    Migaku freeze it sits beside. A file that doesn't exist is a language not yet studied,
    not a freeze."""
    reasons = []
    for lang in langs:
        path = os.path.join(known_morphs_dir, f"gigaku_known_morphs_{lang}.csv")
        if not os.path.exists(path):
            continue
        last = datetime.fromtimestamp(os.path.getmtime(path)).date()
        age = (today - last).days
        if age >= stale_days:
            reasons.append(
                f"known-morphs ({lang}) last written {last} — {age} days ago; its "
                f"refresh has been refusing daily (see /tmp/gigaku-backup.log)."
            )
    return reasons


def _alert(status):
    """A bad run — failed, refused, or stale — goes straight to Telegram; a good one is
    silence. (An absent run can't alert, and nothing watches for one any more — the
    weekly report's "backup: N days ago" line was removed by the user, 2026-08-01.)

    Best-effort, never raises: no network or no credentials must not turn a completed
    backup into a failed command. Plain text, so a reason with odd characters can't
    break the send.

    One 💀 for all three, no per-status icon: the house style for these bot messages is 💀 for
    anything broken and 🌿 for anything fine, and the status word is right there in the
    first line — a distinct emoji per status only spent the reader's attention on telling
    apart what the text already says."""
    status.setdefault("status", "failed")
    status["finished_at"] = _now_iso()
    if status["status"] not in ("failed", "refused", "stale"):
        return
    counts = status.get("counts") or {}
    fate = ("committed" if status.get("committed") else "not committed") + (
        ", pushed" if status.get("pushed") else ", not pushed"
    )
    lines = [
        f"💀 Words backup: {status['status']}",
        status.get("reason", ""),
        fate,
        " · ".join(f"{k} {v:,}" for k, v in counts.items() if v is not None),
    ]
    text = "\n".join(line for line in lines if line)
    try:
        token, chat = telegram.credentials()
        result = telegram.tg_call(token, "sendMessage", {"chat_id": chat, "text": text})
        if result.get("ok"):
            note("Backup alert sent to Telegram.")
        else:
            note(f"Could not send the backup alert: {result.get('description')}")
    except Exception as exc:
        note(f"Could not send the backup alert: {exc}")


def _require_repo(repo):
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise UserError(
            f"{repo} is not a git repository. One-time setup:\n"
            f"  gh repo create <you>/vocab-backup --private\n"
            f"  mkdir -p {repo} && git -C {repo} init\n"
            f"  git -C {repo} remote add origin "
            f"git@github.com:<you>/vocab-backup.git"
        )


def _ensure_gitattributes(repo):
    """Store every file byte-for-byte — a backup must not have git rewrite line endings.

    Without `* -text`, git's autocrlf can normalise CRLF↔LF on commit, so a CRLF source file
    (the user's curated CSVs) would show as changed on every run — endless phantom diffs and,
    worse, a snapshot that isn't a faithful copy. Written once; a user's own version is left
    alone."""
    path = os.path.join(repo, ".gitattributes")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("* -text\n")


def _ensure_gitignore(repo):
    """`_commit` stages with `git add -A`, and Finder drops a .DS_Store wherever it looks —
    which is how one ended up committed to a backup of three datasets. Written once; a
    user's own version is left alone."""
    path = os.path.join(repo, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(".DS_Store\n")


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    if result.returncode:
        raise UserError(
            f"git {' '.join(args)} failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
        )
    return result.stdout


def _has_staged_changes(repo):
    # `git diff --cached --quiet` exits 1 when there ARE staged changes, 0 when none.
    return subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"]).returncode != 0
