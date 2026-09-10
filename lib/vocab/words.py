"""The vocabulary store: import words from Language Reactor + Migaku, merge, cache.

Ported from polyglotka (importer/words.py, importer/words_cache.py,
simple_commands/words_exporter.py), rewritten on the stdlib — pydantic bought validation
gigaku doesn't need for five fields it controls end to end.

A *word* is (text, language) with a learning stage and the time that stage was last
touched. Both sources report the *current* stage of every word they know about, with a
"last modified" timestamp — so the merge rule is simply "latest timestamp wins", and a word
whose latest state is SKIPPED drops out of the store entirely.

The cache is authoritative history. Every import folds the new exports into it, and the
exports are then deleted (settings.RM_PROCESSED_FILES) — neither LR nor Migaku will tell
you what you knew last March, so if the cache goes, the plots' past goes with it.
"""
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from glob import glob

from lib import config
from lib.config import UserError, note, settings


class Stage(StrEnum):
    LEARNING = "LEARNING"
    KNOWN = "KNOWN"
    SKIPPED = "SKIPPED"  # never stored — the merge drops these


# Only these survive a merge, so they're the stages the rest of the code can see.
TRACKED = (Stage.KNOWN, Stage.LEARNING)

# "Known, but nobody recorded when." The year 1 is not a date, it is a flag that reads as
# one: it sorts before every real word, it can never land in a report's week, and it is
# obvious on sight in words.json. AnkiMorphs needs it because Anki stores no such date
# anywhere (see lib/vocab/ankimorphs.py); the alternative was a plausible-looking wrong date,
# which is worse than an obviously absent one. Anything drawing a timeline must fold these
# into the left edge rather than stretch the axis back two millennia — `plots.build` does.
UNKNOWN_DATE = datetime(1, 1, 1)


@dataclass(frozen=True, slots=True)
class Word:
    word: str
    language: str  # lowercase ISO code: 'de', 'ja', …
    stage: Stage
    date: datetime  # when the stage was last modified

    @property
    def key(self):
        return (self.word, self.language)


# ── cache ────────────────────────────────────────────────────────────────────


def _from_json(item):
    return Word(
        word=item["word"],
        language=item["language"].lower(),
        stage=Stage(item["learning_stage"]),
        date=datetime.fromisoformat(item["date"]),
    )


def _to_json(word):
    return {
        "word": word.word,
        "language": word.language,
        "learning_stage": str(word.stage),
        "date": word.date.isoformat(),
    }


def _adopt_legacy_cache():
    """Take over polyglotka's cache, once, if gigaku hasn't got one.

    Same JSON schema (gigaku kept it deliberately), so this is a copy — and it means
    uninstalling polyglotka costs no history.

    The marker is what makes it *once*. Keying only on "gigaku has no cache" would make this
    a resurrection spell: `gigaku clear-cache` deletes the cache, and the next read would
    quietly copy polyglotka's back — clearing nothing for exactly the users this path exists
    for. The marker records that the migration already happened, deleted cache or not."""
    if os.path.exists(config.CACHE_ADOPTED) or os.path.exists(config.WORDS_CACHE):
        return
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    if os.path.exists(config.LEGACY_WORDS_CACHE):
        shutil.copyfile(config.LEGACY_WORDS_CACHE, config.WORDS_CACHE)
        note(f"Adopted polyglotka's word cache → {config.WORDS_CACHE}")
    open(config.CACHE_ADOPTED, "w").close()


def read_cache():
    _adopt_legacy_cache()
    if not os.path.exists(config.WORDS_CACHE):
        return []
    with open(config.WORDS_CACHE, encoding="utf-8") as f:
        return [_from_json(item) for item in json.load(f)]


def write_cache(words):
    path = config.WORDS_CACHE
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"  # write-then-rename: a crash mid-write can't shred the history
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([_to_json(w) for w in words], f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    note(f"Cached {len(words):,} words.")


def clear_cache():
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    open(config.CACHE_ADOPTED, "w").close()  # cleared means cleared — don't re-adopt
    if os.path.exists(config.WORDS_CACHE):
        os.remove(config.WORDS_CACHE)
        note(f"Cleared {config.WORDS_CACHE}")
    else:
        note("Cache is already empty.")


# ── import ───────────────────────────────────────────────────────────────────


def merge(words, keep=()):
    """Fold a stream of (possibly conflicting, possibly repeated) words into the store.

    Latest timestamp per (text, language) wins; SKIPPED means the user un-learned it, so it
    leaves the store rather than lingering at its previous stage.

    `keep` is the one exception, and it exists because **Migaku's SKIPPED is two signals
    wearing one name**. It is what UNKNOWN and IGNORED both map to, and measured on the
    Japanese list those are 2,386 and **7**: "I have seen this word and you have not marked
    it" versus "never show me this again". Only the second is a claim about knowledge. So a
    word AnkiMorphs currently asserts you know was being deleted by Migaku's silence — 190
    of them at once, and a fresh crop every week as Anki runs ahead of Migaku (this is the
    structural version of a gap first patched by hand in Migaku's own UI, 2026-08-01).

    Pass the keys another source currently claims as known and their SKIPPED record loses:
    the word keeps its latest *tracked* state instead of leaving. Migaku can still un-learn
    its own words — nothing else changes, and the timestamp rule is untouched everywhere.
    """
    keep = set(keep)
    latest, tracked = {}, {}
    for word in words:
        current = latest.get(word.key)
        if current is None or word.date >= current.date:
            latest[word.key] = word
        if word.stage in TRACKED:
            best = tracked.get(word.key)
            if best is None or word.date >= best.date:
                tracked[word.key] = word

    out = []
    for key, word in latest.items():
        if word.stage in TRACKED:
            out.append(word)
        elif key in keep and key in tracked:
            out.append(tracked[key])
    return sorted(out, key=lambda w: (w.language, w.word))


def import_words(cache_allowed=True, consume=False):
    """The word list every command runs on: cache + whatever new exports are lying around.

    `consume` is what separates `gigaku import` from everything else. Every command picks up
    new words (so the plots you draw are never stale), but only `import` — the command whose
    *job* is importing — is allowed to then **delete the export files** and rewrite the
    known-morphs CSVs. `gigaku words --lang de` is a read: it must not quietly bin the LR
    export you were about to look at.

    `cache_allowed=False` (also `import`) turns "nothing new to import" into an error instead
    of a silent success.
    """
    from lib.vocab import lr, migaku

    lr_files = sorted(glob(os.path.join(settings.EXPORTED_FILES_DIR, settings.LR_WORDS_GLOB)))
    migaku_files = sorted(glob(os.path.join(settings.EXPORTED_FILES_DIR, settings.MIGAKU_WORDS_GLOB)))

    cached = read_cache()
    fresh = []
    fresh += lr.read_exports(lr_files)
    fresh += migaku.read_exports(migaku_files)
    # Migaku's own export is a userscript nobody wants to run monthly; reading Chrome's
    # IndexedDB gives the same word list with no clicks. CSVs, if present, still win.
    chrome_failed = ""
    if settings.CHROME and not migaku_files:
        try:
            fresh += migaku.read_chrome()
        except UserError as exc:
            # No Chrome, no Migaku in it, signed out — none of that should sink a command
            # that has a perfectly good cache to fall back on (`plots`, `kanji`, `words`).
            chrome_failed = str(exc)
            note(f"Skipping Migaku in Chrome: {exc}")

    # AnkiMorphs is the third source, and the odd one out: it has nothing to consume and no
    # export to find, so it is asked every time rather than gated on files being present.
    # Best-effort like Chrome — Anki not running must not sink `plots` or `kanji` — and it
    # is handed the words already cached for its language, because it only ever *adds*
    # (see lib/vocab/ankimorphs.py, rule 4: a word the cache dates already keeps its date).
    asserted = set()
    if settings.ankimorphs_langs():
        from lib.vocab import ankimorphs

        already = {lang: {w.word for w in cached if w.language == lang}
                   for lang in settings.ankimorphs_langs()}
        if "de" in already:
            # The de CSV's own echo must not read as new words — see _de_exported_forms.
            already["de"] |= _de_exported_forms()
        try:
            # `asserted` is the *whole* known set as (word, lang) keys, not just what was
            # emitted: a morph is left unemitted precisely because the cache already holds
            # it, and those are exactly the ones Migaku's SKIPPED would delete out from
            # under it.
            anki_words, asserted = ankimorphs.read_known(known=already)
            fresh += anki_words
        except UserError as exc:
            note(f"Skipping AnkiMorphs: {exc}")

    if not fresh:
        nothing = (
            f'No exports found in "{settings.EXPORTED_FILES_DIR}" '
            f'("{settings.LR_WORDS_GLOB}", "{settings.MIGAKU_WORDS_GLOB}")'
            + ("" if settings.CHROME else " and Chrome import is off")
        )
        if chrome_failed:
            nothing += f", and Migaku in Chrome could not be read: {chrome_failed}"
        if not cache_allowed:
            raise UserError(nothing + ".")
        if not cached:
            raise UserError(f"{nothing}, and the cache is empty: {config.WORDS_CACHE}")
        return cached

    words = merge(cached + fresh, keep=asserted)
    write_cache(words)

    if consume:
        for lang in settings.save_langs():
            save_known_morphs(lang, words)
        remove_processed(lr_files + migaku_files)

    return words


def remove_processed(files):
    if not settings.RM_PROCESSED_FILES:
        return
    for path in files:
        os.remove(path)
        note(f'Removed "{path}".')


# ── queries ──────────────────────────────────────────────────────────────────


def languages(words):
    return sorted({w.language for w in words})


def word_list(words, lang, stage=""):
    """The words of one language, alphabetically. `stage` empty means known + learning."""
    langs = languages(words)
    if lang not in langs:
        raise UserError(f"--lang must be one of {', '.join(langs)}, not {lang!r}")
    stage = stage.upper()
    if stage and stage not in TRACKED:
        raise UserError(f"--stage must be known or learning, not {stage.lower()!r}")
    return sorted(w.word for w in words if w.language == lang and (not stage or w.stage == stage))


def _spacy_python():
    """The AnkiMorphs spaCy venv's python — the same model recalc matches against.

    Version-sorted numerically, because "3_13" < "3_9" as strings — a lexicographic pick
    would hand a stale 3.9 venv the job the day a new one appears beside it. And a hit
    must actually run: glob happily returns a venv whose interpreter is a dangling
    symlink, which is exactly what an Anki python bump leaves behind (the venv is built
    against uv's cpython; the bump moves it), and that failure deserves the "no venv"
    UserError, not a FileNotFoundError out of subprocess."""
    import re
    from glob import glob as _glob

    def version(path):
        return [int(n) for n in re.findall(r"\d+", path.rsplit("python-", 1)[-1])]

    hits = [p for p in _glob(os.path.expanduser(
        "~/Library/Application Support/Anki2/addons21/spacy-venv-python-*/bin/python"))
            if os.path.exists(p)]  # exists() follows symlinks; a dangling one only lexists
    return max(hits, key=version) if hits else None


_DE_LEMMA_SCRIPT = """\
import json, sys
import spacy
nlp = spacy.load("de_core_news_md", disable=["parser", "ner"])
words = json.load(sys.stdin)
out = {}
for w, doc in zip(words, nlp.pipe(words, batch_size=256)):
    tokens = [t for t in doc if not t.is_space]
    out[w] = tokens[0].lemma_ if len(tokens) == 1 else w
print(json.dumps(out))
"""


def _de_lemmas(known):
    """Migaku dictForms → the spaCy lemmas AnkiMorphs will actually match, cached.

    Migaku lowercases German (`abend`) while recalc's lemmas capitalize nouns (`Abend`) —
    measured 2026-08-06, 73% of a raw dump is inert. Normalisation runs through the
    AnkiMorphs spaCy venv itself (subprocess — the same no-new-dependency shape as
    `gigaku clarity` riding the claude CLI), and every answer is cached, so a daily run
    only pays for the words Migaku learnt since yesterday.
    """
    import subprocess

    try:
        with open(config.DE_LEMMA_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    missing = [w for w in known if w not in cache]
    if missing:
        python = _spacy_python()
        if not python:
            raise UserError("no AnkiMorphs spaCy venv (addons21/spacy-venv-python-*)")
        proc = subprocess.run([python, "-c", _DE_LEMMA_SCRIPT], input=json.dumps(missing),
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise UserError(f"spaCy lemmatisation failed: {proc.stderr.strip()[:200]}")
        # spaCy occasionally answers with punctuation instead of a word (`akt` → "---",
        # `al` → "--"; 12 of 8,059 measured). Those literals became CSV rows, then
        # AnkiMorphs "known morphs" no script claims — named on every backup run, forever,
        # while the real Akt/Al never got a usable row. A lemma with no letter is no lemma.
        cache.update({w: lem if any(ch.isalpha() for ch in lem) else w
                      for w, lem in json.loads(proc.stdout).items()})
        tmp = config.DE_LEMMA_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, config.DE_LEMMA_CACHE)
        note(f"Lemmatised {len(missing):,} new German word(s) for known-morphs.")
    return sorted({cache[w] for w in known})


def _de_exported_forms():
    """Every form the de CSV ever exported, lowered — the import must recognise its own echo.

    The export writes spaCy lemmas with their capitals (`alarmsystem` → `Alarmsyst`) and the
    import lowers what AnkiMorphs knows (`_CACHE_FORM`), so for any word whose lemma is not
    itself the read hands back a form the cache never held — a brand-new "word" at the year 1,
    which the next export re-lemmatises into yet another form (aborigine → aboriginen →
    Aborigin took two cycles). Measured 2026-08-08: 594 fabricated de words in three days,
    growing ~80/day, while ja — whose export writes cache forms verbatim — closed the loop by
    construction. The lemma cache is precisely the record of what was exported (keys are cache
    words, values the CSV rows), so its lowered keys ∪ values is "already represented" — and
    it covers the window after a cache cleanup when the Morphs db still holds the retired
    lemmas until the next recalc, which is why a cleanup must prune words but keep this cache.
    """
    try:
        with open(config.DE_LEMMA_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return set()
    return {k.lower() for k in cache} | {v.lower() for v in cache.values()}


def _csv_rows(path):
    """Data rows of an existing known-morphs CSV — 0 for a missing or unreadable file."""
    try:
        with open(path, encoding="utf-8") as f:
            return max(0, sum(1 for line in f if line.strip()) - 1)  # minus the header
    except OSError:
        return 0


def save_known_morphs(lang, words):
    """Write the AnkiMorphs known-morphs CSV for one language.

    https://mortii.github.io/anki-morphs/user_guide/usage/known-morphs-exporter.html

    A language you've configured but haven't studied yet is not an error — it's a Tuesday.
    Say so and move on, rather than aborting the import (mid-way, before the exports are
    cleaned up) with a complaint about a `--lang` flag that was never passed.

    German goes through `_de_lemmas` (the case boundary, see lib/vocab/ankimorphs.py) —
    and on any failure there the existing file STANDS: overwriting a good normalized CSV
    with a raw lowercase dump would silently unmark every noun at the next recalc. The
    except-set is what the subprocess path actually throws, not just UserError: a dangling
    venv symlink is an OSError, a garbled answer a ValueError, a hung model a
    TimeoutExpired — and the first uncaught one didn't keep any file, it took the whole
    daily backup down before `_gather` ever ran.

    The write gets the same protections the backup repo has, because this file is what
    recalc reads: an empty list (a language left holding only LEARNING words) or a
    collapse past BACKUP_SHRINK_PCT keeps the existing file and says so — a hollow CSV
    would silently unmark thousands of morphs at the next R, and `_gate` only guards the
    *repo copy*, after Anki's copy was already overwritten. Atomic for the same reason:
    recalc reading a half-written file is the same accident at a smaller size.
    """
    import subprocess

    if lang not in languages(words):
        note(f"No {lang} words yet — skipping its known-morphs file.")
        return
    known = [w.word for w in words if w.language == lang and w.stage == Stage.KNOWN]
    if lang == "de":
        try:
            known = _de_lemmas(known)
        except (UserError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            note(f"known-morphs (de): {exc} — keeping the existing file.")
            return
    path = os.path.join(settings.KNOWN_MORPHS_DIR, f"gigaku_known_morphs_{lang}.csv")
    if not known:
        note(f"known-morphs ({lang}): no KNOWN words — keeping the existing file.")
        return
    prev = _csv_rows(path)
    if prev and len(known) < prev * (1 - settings.BACKUP_SHRINK_PCT / 100):
        note(f"known-morphs ({lang}): refusing to shrink {prev:,} → {len(known):,} rows "
             f"(past {settings.BACKUP_SHRINK_PCT}%; raise GIGAKU_BACKUP_SHRINK_PCT if "
             f"this is a real mass-unlearn) — keeping the existing file.")
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("Morph-Lemma\n" + "\n".join(known) + "\n")
    os.replace(tmp, path)
    note(f'Saved {len(known):,} known morphs ({lang}): "{path}".')
