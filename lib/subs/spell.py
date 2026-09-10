"""Proofread a ripped German Primary before anything reads it, through Claude.

`gigaku subs` rips its Primary from Language Reactor's *ASR Pro German* — a machine transcript
of the German dub. `lib/subs/spell_prompt.py` holds what was measured about the mistakes in it
and the rubric that answers them; this module is everything that touches the world: macOS's
German dictionary, the `claude` CLI, the files, and the record that makes a pass undoable.

Four things this is responsible for, none of which belong in the rubric:

* **Timecodes cannot move.** The Primary is read back through `lib/subs/srt.py`, whose cues
  carry their timecode line verbatim, and only `text` is replaced. There is no arithmetic on a
  timestamp anywhere in this pass, so the two tracks cannot drift apart by construction.
* **A pass is undoable.** The ripped file is copied to the cache byte for byte *before* the
  first request is made, and every change is journalled with the line it replaced. `--revert`
  restores the copy; it does not reconstruct anything.
* **A name is settled once for a whole series.** The ledger (`ledger()`) is what turns the
  commonest defect in these files — the same character spelled `Esdeath`, `Esteth`, `Estef`,
  `Estet` and `Esdeth` inside one show — into a fact the model is told rather than a pattern
  it would have to infer from cues it cannot see. It is built from every episode of the show
  on disk, so episode 1 of a sweep is as well informed as episode 13.
* **It never costs an episode.** Everything here is best-effort: a missing `claude`, a missing
  dictionary, a refused answer or a wild one all end with the German exactly as it was ripped
  and a note saying so. The Primary is already on disk and readable, and a proofreader that
  could *fail* an episode would be a new way to lose work the browser already paid for. The
  one exception is the record — see `episode`.
"""
import collections
import json
import os
import re
import shutil
import threading
from hashlib import sha1
from pathlib import Path

from lib import claude
from lib.config import CACHE_DIR, UserError, note, settings
from lib.subs import library, spell_prompt, srt

WORK_DIR = os.path.join(CACHE_DIR, "subs-spell")
VERSION = 1

# A word as this pass counts one: letters, plus the hyphen and apostrophe that live inside
# German and transliterated Korean names (`Si-heon`, `geht's`).
_WORD = re.compile(r"[^\W\d_]+(?:[''\-][^\W\d_]+)*", re.UNICODE)
# What ends a sentence, so the capital after it is grammar rather than a name — the same
# one-sided reading `translate_prompt.names` makes, and for the same reason.
_SENTENCE_END = re.compile(r"[.!?…]['\"»)]*\s*$")

# How often a spelling must be said before it can be the **canonical** one. A name the ledger
# settles is imposed on every episode of the series, so it has to be well attested.
LEDGER_MIN = 3
# How often a spelling must be said to be offered as a possible **variant**. Deliberately lower
# than LEDGER_MIN, and that asymmetry is the point — measured on Crash Landing on You, where a
# character is called `Samsuk` 4 times and misheard as `Samsung` twice, `Samso` twice and
# `Sambuk` twice. Under one shared floor of 3 none of the mishearings was even a candidate, so
# no cluster formed, the ledger said nothing, and the pass repaired **1 of the 4** occurrences
# from context alone — a recall miss on the corpus's largest defect class. A variant heard
# twice is still a variant; it is the canonical that must clear the bar, not the mistake. The
# cost is a longer candidate list (20 → 51 on that show) and it is one small call either way,
# while `parse_ledger`'s guards plus the LEDGER_MIN test on the head bound what can come back.
VARIANT_MIN = 2
# How close a real German word must sit to a name before it is treated as a spelling of that
# name. Two, because that is `Guru`/`Guro` (1) and `Samsung`/`Samsuk` (2) while leaving the
# grammar alone — and because a pulled-in word can only ever become canonical by clearing the
# MAJORITY margin (`spell_prompt.parse_ledger`'s `weak`), so being wrong here costs a redundant
# line in one request rather than a renamed character.
PULL_IN_EDITS = 2
# …but scaled to the seed's length, and never from a seed shorter than this. A flat distance of
# two is a *different word* on a three-letter name and a slip on an eight-letter one, and the
# flat rule was measured floundering: it pulled 78 ordinary German words into Crash Landing on
# You's 51 candidates — `Bad`, `Bein`, `Berg`, `Bier`, `Boot`, `Boss` — which is exactly the
# grammar flooding the dictionary filter exists to prevent. Scaled, `Guro`(4) reaches `Guru`
# at one edit and `Samsuk`(6) reaches `Samsung` at two, while a short seed reaches almost
# nothing.
PULL_IN_MIN_LEN = 4


def _slack(seed):
    return max(1, min(PULL_IN_EDITS, len(seed) // 3))


def _edits(a, b):
    """Levenshtein. Small strings only — this runs over a series' candidate names."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]

_LOCAL = threading.local()


# ── macOS's German dictionary ────────────────────────────────────────────────

def _checker():
    """A private NSSpellChecker for this thread.

    Not `sharedSpellChecker()`: this runs on `_Glosser`'s worker (see `subs._gloss`), and the
    shared checker is main-thread-affine in principle. A private instance per thread costs
    nothing and removes the question. If it ever misbehaves anyway the fix is not a lock but a
    shape change — nominate in the main thread during the rip, where it is pure and instant,
    and hand the verdicts to the worker with the file.
    """
    checker = getattr(_LOCAL, "checker", None)
    if checker is None:
        from AppKit import NSSpellChecker          # imported at call time — see the docstring
        checker = _LOCAL.checker = NSSpellChecker.alloc().init()
        if "de" not in [str(x) for x in checker.availableLanguages()]:
            raise UserError(
                "macOS has no German spelling dictionary — turn German on in "
                "System Settings ▸ Keyboard ▸ Text Input ▸ Spelling"
            )
    return checker


def unknown_words(words, cache=None):
    """The subset of ``words`` macOS's German dictionary does not recognise.

    Checked one bare word at a time rather than over the running text, so a verdict is a
    property of the word alone and can be cached across the whole show — an episode re-checks
    a few hundred new tokens instead of a few thousand seen ones.

    This is a *nomination*, never a decision. Measured on the ripped corpus, ~54 of the ~63
    tokens it flags per episode are proper names, and its own suggestions are actively
    dangerous (`Barou`→`Balou`, `Susanoo`→`Susana`, `Reo`→`Leo`, `Midgar`→`Midgard`), which is
    why they are never passed on. What it is genuinely good at is the two classes a model
    reading prose glides over: a noun left lowercase (`ankunft` flags, `Ankunft` does not) and
    a one-letter slip inside a long word.
    """
    cache = {} if cache is None else cache
    checker = _checker()
    out = []
    for word in words:
        verdict = cache.get(word)
        if verdict is None:
            found, _ = checker.checkSpellingOfString_startingAt_language_wrap_inSpellDocumentWithTag_wordCount_(
                word, 0, "de", False, 0, None)
            verdict = cache[word] = bool(found.length) and found.location < len(word)
        if verdict:
            out.append(word)
    return out


# ── where the record lives ───────────────────────────────────────────────────

def _show_dir(primary) -> Path:
    return Path(WORK_DIR) / Path(primary).parent.name


def work_path(primary) -> Path:
    """The journal: what was changed in this episode, and what it replaced."""
    return _show_dir(primary) / f"{Path(primary).stem}.json"


def orig_path(primary) -> Path:
    """The Primary exactly as Language Reactor exported it. What `--revert` restores."""
    return _show_dir(primary) / f"{Path(primary).stem}.orig.srt"


def dict_path(primary) -> Path:
    """Cached dictionary verdicts for this show's words. A verdict never changes."""
    return _show_dir(primary) / "words.json"


# Nothing above goes anywhere near `chrome/gigaku/subs/`. A sidecar there would be invisible
# to `library.scan` (which matches only `… - Primary.srt` / `… - Secondary.srt`) *and* would
# survive `excel_to_srt.trash_existing`, which matches the same pattern — permanent debris in
# a directory the Chrome extension serves. The cache is where `translate.work_path` puts its
# work file for the first half of that reason; this pass has both.


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    os.replace(tmp, path)


def body(cues) -> str:
    """The text of every cue, for hashing. Timecodes are excluded on purpose: this is asked
    "has the German moved", and a re-render that touched nothing must answer no."""
    return "\n".join(cue.text for cue in cues)


def _sha(text) -> str:
    return sha1(text.encode("utf-8")).hexdigest()[:12]


# ── the show's name ledger ───────────────────────────────────────────────────

def _mentions(cues):
    """Capitalised words that are not opening a sentence, with their counts.

    One-sided by design, exactly as `translate_prompt.names` is: a sentence-initial capital is
    dropped, because otherwise every `Der` and `Was` would crowd out the cast, and the cost of
    the two mistakes is not symmetric — an ordinary word that slips in is one redundant line in
    the request, a name left out is the inconsistency the ledger exists to prevent.
    """
    counts = {}
    for cue in cues:
        for match in _WORD.finditer(cue.text):
            word = match.group()
            if not word[:1].isupper():
                continue
            if match.start() == 0 or _SENTENCE_END.search(cue.text[:match.start()]):
                continue          # grammar's capital, not a name's
            counts[word] = counts.get(word, 0) + 1
    return counts


def candidates(cues_by_file, cache=None):
    """Recurring capitalised words this series' German dictionary does not know, with counts.

    The dictionary filter is what makes the list a list of names. Without it the loudest
    clusters in the corpus are grammar — `Wenn`/`Denn`, `Wie`/`Wieso`, `Meine`/`Eine`/`Deine` —
    and the ledger fills with function words that must never be "settled" as anything.
    """
    counts = {}
    for cues in (cues_by_file or {}).values():
        for word, n in _mentions(cues).items():
            counts[word] = counts.get(word, 0) + n
    counts = {w: n for w, n in counts.items() if n >= VARIANT_MIN}
    unknown = set(unknown_words(sorted(counts), cache))

    # **A name that collides with a real German word is invisible to the dictionary filter, and
    # that filter is otherwise the only thing keeping grammar out of the ledger.** Measured on
    # Move to Heaven: the protagonist is written `Guru` 26 times and `Guro` 10, and `Guru` is
    # also the German word for a guru — so it was never a candidate, the ledger saw only
    # `Guro`, and the pass moved three occurrences the *wrong* way, from the majority spelling
    # to the minority one. Crash Landing on You has the same shape with `Samsung`, which macOS
    # knows as a brand. Neither is rare: dubbed Korean and Japanese names land on German words
    # constantly.
    #
    # So the unknown words are only the **seeds**. A word the dictionary does know is pulled in
    # when it sits within `PULL_IN_EDITS` of a seed — close enough to be the same name misheard,
    # far from `Wenn`/`Denn`, which no seed is near. Its own count comes with it, which is the
    # whole point: `Guru` arrives with 26 and outranks the seed that found it.
    seeds = [w for w in sorted(unknown) if len(w) >= PULL_IN_MIN_LEN]
    for word in sorted(set(counts) - unknown):
        if any(_edits(word.lower(), seed.lower()) <= _slack(seed) for seed in seeds):
            unknown.add(word)
    return {w: n for w, n in counts.items() if w in unknown}


def ledger(primary, cues_by_file=None, cache=None, conf=None, ask=True):
    """``{correct spelling: [misspellings of it]}`` for this whole series.

    **Why a model decides this and not a similarity threshold.** Grouping the variants
    mechanically was tried first and measured on the real corpus, and there is no threshold
    that works — the pairs that must merge score 0.50 to 0.92 (`Esdeath`/`Estef` 0.50,
    `Esdeath`/`Esteth` 0.77, `Tatsumi`/`Ttsumi` 0.92) and the pairs that must **not** score
    0.25 to 0.80 (`Koro`/`Kuro` 0.75, `Enma`/`Emma` 0.75, `Kurome`/`Kuro` 0.80,
    `Nagenda`/`Narzenda` 0.80 — that last one *should* merge). The ranges overlap completely,
    so any cutoff either leaves `Estef` uncorrected or renames a character. Which is fatal
    here rather than merely disappointing: the canonical names are handed to
    `spell_prompt.refuse` as spellings that may not be changed, so a wrong group does not miss
    a fix, it **protects a mistake in every episode of the series at once**.

    Asked of a model instead, it is the narrowest kind of question there is — a list of words
    and their counts, no scene, no grammar, nothing to weigh — which is the same argument
    `translate._normalise` records for moving its job out of the glosser's rubric.

    **One call per series, not per episode.** It is built from every Primary of the show on
    disk, cached under the candidate list's own hash, so a thirteen-episode sweep pays once
    and episode 1 knows the cast exactly as well as episode 13 does. A new episode landing
    changes the candidates and re-asks; nothing else does.

    Best-effort: if the call fails the series simply has no ledger, and the pass still
    proofreads everything else. An empty ledger is a worse pass, never a wrong one.

    ``ask=False`` answers from the cache or not at all — what `--dry-run` uses, so the flag
    documented as costing nothing keeps costing nothing.
    """
    conf = conf or settings
    counts = candidates(cues_by_file, cache)
    if len(counts) < 2:
        return {}

    path = _show_dir(primary) / "names.json"
    stamp = _sha(repr(sorted(counts.items())))
    stored = _read_json(path, {})
    if stored.get("fingerprint") == spell_prompt.LEDGER_FINGERPRINT \
            and stored.get("candidates") == stamp:
        return {k: list(v) for k, v in (stored.get("names") or {}).items()}

    if not ask:
        return {}
    try:
        text, usage, cost = claude.ask(
            spell_prompt.render_ledger(counts), spell_prompt.LEDGER, conf.SUBS_SPELL_MODEL,
            what=f"{len(counts)} names", effort=conf.SUBS_SPELL_EFFORT,
        )
    except UserError as exc:
        note(f"    (no name ledger for this series — {exc})")
        return {}
    weak = set(counts) - set(unknown_words(sorted(counts), cache))
    names = spell_prompt.parse_ledger(text, counts, weak)
    # A group whose own canonical is barely attested settles nothing: it is two rare spellings
    # of something, and picking one to impose on the whole series would be a guess.
    names = {k: v for k, v in names.items() if counts.get(k, 0) >= LEDGER_MIN}
    note(f"    name ledger: {len(names)} name(s) with variants, of {len(counts)} candidates"
         f" ({claude.describe(usage)}, ${cost:.2f})")
    _write_json(path, {"version": VERSION, "fingerprint": spell_prompt.LEDGER_FINGERPRINT,
                       "candidates": stamp, "counts": counts, "names": names})
    return names


def ledger_lines(names):
    """The ledger as the request shows it — one name per line, its misspellings after it."""
    return [f"{canonical} (never {', '.join(variants)})" if variants else canonical
            for canonical, variants in sorted(names.items())]


def _show_cues(primary):
    """Every episode of this show **as Language Reactor ripped it**, parsed.

    The cached `.orig.srt` wherever this pass has already been, the live Primary everywhere
    else — which is the same thing, since an unproofread Primary *is* the ripped German.

    **This has to read the ripped text, and the first version reading the live files was
    wrong in a way that hid itself — measured on A Time Called You, 2026-09-04.** The ledger
    decides a name by how often each spelling is written; built from the corrected files, it
    reads a corpus its own edits have already rewritten, so the spelling it picked last
    episode is the spelling it sees winning this episode. Untouched, the show had `Sihon` 11
    against `Siheon` 12 — a one-vote margin, which is a coin toss and not evidence. Five
    episodes later the same count read 4 against 33, a gap the pass had manufactured entirely
    out of its own corrections. It happened to have picked the right name, and that was luck.
    A wrong first choice would have been confirmed just as hard and just as invisibly.

    Two more things the loop broke, both visible in the same log. The candidate list changed
    after every episode, so the cache key changed, so the ledger was **re-asked once per
    episode rather than once per series** — 6 calls for 6 episodes. And because corrected
    variants fall below `LEDGER_MIN`, the ledger *shrank* as the sweep went: 6 settled names
    at episode 1, 3 by episode 5, so the last episode of a season was proofread with half the
    protection of the first. Reading the ripped text fixes all three at once, and makes a
    re-run reproduce the ledger it had before, which is what lets a rubric be A/B'd at all.
    """
    out = {}
    for path in sorted(Path(primary).parent.glob("* - Primary.srt")):
        ripped = orig_path(path)
        try:
            out[path.name] = srt.read(ripped if ripped.exists() else path)
        except OSError:
            continue
    return out


# ── the pass ─────────────────────────────────────────────────────────────────

def apply(cues, changes):
    """The episode with ``{index: text}`` applied. Pure — no dictionary, no model, no files.

    Only `text` is rebuilt; `index` and `timecode` are the objects that came off disk. An
    index that was never in the file cannot enter it, because the walk is over the cues.
    """
    return [srt.Cue(cue.index, cue.timecode, changes.get(cue.index, cue.text)) for cue in cues]


def episode(primary, *, conf=None, redo=False) -> dict:
    """Proofread one episode's Primary in place. **Never raises.**

    Returns ``{"cues", "changed", "refused", "calls", "cost", "flagged_before",
    "flagged_after", "why"}``; ``why`` is set when the pass stood down.

    **The one thing here that is not best-effort is the record.** The order is fixed —
    `.orig.srt`, then the journal, then the corrected Primary — and a failure to write either
    of the first two abandons the pass with the German untouched. A correction with no receipt
    is precisely the state that must not exist; the reverse order would leave a corrected file
    with no way back, which is worse than not correcting it.
    """
    conf = conf or settings
    primary = Path(primary)
    stats = {"cues": 0, "changed": 0, "refused": 0, "calls": 0, "cost": 0.0,
             "flagged_before": 0, "flagged_after": 0, "why": None}
    try:
        cues = srt.read(primary)
    except OSError as exc:
        stats["why"] = str(exc)
        return stats
    stats["cues"] = len(cues)
    if not cues:
        stats["why"] = "no cues"
        return stats

    source = _sha(body(cues))
    record = _read_json(work_path(primary), {})

    # **A re-read starts from the German as it was ripped, not from the last pass's output.**
    # Corrections would otherwise compound — a second pass proofreading a first pass's answers
    # — and, worse, two rubrics could never be compared, because each would be reading a
    # different file. The tuning loop is exactly `--redo` after an edit, so that matters here
    # more than anywhere. Guarded the way `revert` is: only a Primary that still hashes to what
    # this pass wrote is replaced, so a hand edit or a re-rip is never quietly discarded.
    if redo and orig_path(primary).exists() and record.get("result_sha") == source:
        try:
            cues = srt.read(orig_path(primary))
            source = _sha(body(cues))
            note("  re-reading from the German as it was ripped")
        except OSError:
            pass

    if not redo and record.get("version") == VERSION \
            and record.get("fingerprint") == spell_prompt.FINGERPRINT \
            and record.get("result_sha") == source:
        stats["changed"] = len(record.get("edits") or [])
        stats["why"] = "already proofread"
        return stats

    try:
        words_cache = _read_json(dict_path(primary), {})
        shown = _show_cues(primary)
        names = ledger(primary, shown, words_cache)
        # The copy comes before the first request, not after the last: everything between them
        # can fail, and none of it may leave the ripped German unreachable.
        if not orig_path(primary).exists():
            orig_path(primary).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(primary, orig_path(primary))

        changes, refusals, spent, calls = _proofread(
            cues, names, words_cache, conf, word_counts(shown))
        stats.update(refused=len(refusals), cost=spent, calls=calls)
    except UserError as exc:
        stats["why"] = str(exc)
        note(f"  (spelling left as ripped — {exc})")
        return stats
    except Exception as exc:                       # noqa: BLE001 — AppKit is an OS surface
        stats["why"] = f"{type(exc).__name__}: {exc}"
        note(f"  (spelling left as ripped — {stats['why']})")
        return stats

    fixed = apply(cues, changes)
    result = _sha(body(fixed))
    before = len(unknown_words(sorted(_tokens(cues)), words_cache))
    after = len(unknown_words(sorted(_tokens(fixed)), words_cache))
    stats.update(changed=len(changes), flagged_before=before, flagged_after=after)

    _write_json(dict_path(primary), words_cache)
    _write_json(work_path(primary), {
        "version": VERSION,
        "fingerprint": spell_prompt.FINGERPRINT,
        "model": conf.SUBS_SPELL_MODEL,
        "source_sha": source,
        "result_sha": result,
        "cues": len(cues),
        "flagged_before": before,
        "flagged_after": after,
        "ledger": names,
        "edits": [{"cue": cue.index, "before": cue.text, "after": changes[cue.index]}
                  for cue in cues if cue.index in changes],
        "refused": refusals,
    })
    if changes:
        srt.write(primary, fixed)                  # atomic, and last
    return stats


def _tokens(cues):
    return {m.group() for cue in cues for m in _WORD.finditer(cue.text)}


def word_counts(cues_by_file):
    """Every word of the series and how often it is written that way.

    **Total occurrences, unlike `_mentions`** — which drops sentence-initial capitals because
    it is answering "is this a name", a different question. This one answers "how is this name
    usually spelled", and a character is addressed at the start of a sentence more than
    anywhere else, so dropping those would understate exactly the spelling that matters.
    """
    counts = collections.Counter()
    for cues in (cues_by_file or {}).values():
        for cue in cues:
            counts.update(m.group() for m in _WORD.finditer(cue.text))
    return counts


def _proofread(cues, names, words_cache, conf, counts=None):
    """Every line of the episode, in windows. Returns (changes, refusals, cost, calls)."""
    views = srt.groups(cues, conf.SUBS_SPELL_GROUP, conf.SUBS_SPELL_CONTEXT)
    changes, refusals, spent, calls = {}, [], 0.0, 0
    protected = set(names)
    note(f"  proofreading {len(cues)} line(s) in {len(views)} request(s)"
         f" — {conf.SUBS_SPELL_MODEL}"
         + (f", {len(names)} settled name(s)" if names else ""))
    for number, view in enumerate(views, 1):
        asked = [cue for cue, needed in view if needed]
        flagged = unknown_words(
            sorted({m.group() for cue in asked for m in _WORD.finditer(cue.text)}), words_cache)
        got, refused, cost, made = _ask(view, ledger_lines(names), flagged, protected,
                                        conf, counts)
        changes.update(got)
        refusals += refused
        spent += cost
        calls += made
        note(f"  request {number}/{len(views)}: {len(got)} fix(es)"
             + (f", {len(refused)} refused" if refused else ""))
    return changes, refusals, spent, calls


def _ask(view, names, flagged, protected, conf, counts=None):
    """One request, plus up to ``SUBS_SPELL_RETRIES`` re-asks for whatever didn't come back.

    A re-ask sends the *same* view with only the unanswered ids marked (`srt.regroup`), so the
    line is still proofread with the scene around it — the seam argument `translate._ask`
    makes, and it matters more here, where a cue cut mid-clause is the commonest reason a line
    looks wrong when it is fine.
    """
    changes, refusals, spent, calls = {}, [], 0.0, 0
    for attempt in range(max(0, int(conf.SUBS_SPELL_RETRIES)) + 1):
        wanted = [cue.index for cue, needed in view if needed]
        if not wanted:
            break
        text, usage, cost = claude.ask(
            spell_prompt.render(view, names, flagged), spell_prompt.SYSTEM,
            conf.SUBS_SPELL_MODEL, what=f"{len(wanted)} subtitle lines",
            effort=conf.SUBS_SPELL_EFFORT,
        )
        spent += cost
        calls += 1
        got, refused = spell_prompt.parse(text, view, protected, counts)
        changes.update(got)
        refusals += refused
        missing = spell_prompt.unanswered(text, view)
        note(f"    …{len(wanted) - len(missing)}/{len(wanted)} answered"
             f" ({claude.describe(usage)})")
        if not missing or attempt >= conf.SUBS_SPELL_RETRIES:
            if missing:
                # Not an error, and this is the deliberate divergence from the glosser: an
                # unanswered line simply stands as it was ripped, which is the state the file
                # was already in. Said out loud so a truncated reply is still visible.
                note(f"    ({len(missing)} line(s) unanswered — left as ripped)")
            break
        note(f"    re-asking {len(missing)} line(s) that didn't come back")
        view = srt.regroup(view, missing)
    return changes, refusals, spent, calls


# ── revert and read-back ─────────────────────────────────────────────────────

def revert(primary) -> bool:
    """Put the German back exactly as Language Reactor exported it.

    Refuses an episode whose Primary no longer hashes to what this pass wrote — a re-rip, a
    hand edit or a second corrective pass all land there, and restoring a stale copy over one
    of those is the one outcome nothing can undo.
    """
    primary = Path(primary)
    original, record = orig_path(primary), _read_json(work_path(primary), {})
    if not original.exists():
        note(f"  {primary.name}: nothing to revert to")
        return False
    try:
        current = _sha(body(srt.read(primary)))
    except OSError as exc:
        note(f"  {primary.name}: {exc}")
        return False
    if record.get("result_sha") and current != record["result_sha"]:
        if current == record.get("source_sha"):
            note(f"  {primary.name}: already as ripped")
            return False
        note(f"  {primary.name}: has changed since it was proofread — not reverting")
        return False
    shutil.copyfile(original, primary)
    note(f"  {primary.name}: reverted")
    return True


def journal(primary):
    """This episode's record, for `gigaku spell --show`."""
    return _read_json(work_path(primary), {})


# ── `gigaku spell` ───────────────────────────────────────────────────────────

def pending(root, name=None, season=None, episodes=None, redo=False):
    """Primaries in the library that want proofreading. Returns [Path, …].

    The library is the source of truth rather than a list of what was ripped, the rule
    `translate.pending` already follows: a Primary is a Primary however it got there.
    """
    root = Path(root)
    out = []
    for show in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        if name and name.lower() not in show.name.lower():
            continue
        for primary in sorted(show.glob("* - Primary.srt")):
            base = primary.name[: -len(" - Primary.srt")]
            _, base_season, number = library.parse_base(base)
            if season is not None and (base_season or 0) != season:
                continue
            if episodes is not None and not episodes.contains(number):
                continue
            if not redo:
                record = journal(primary)
                if record.get("fingerprint") == spell_prompt.FINGERPRINT:
                    try:
                        if record.get("result_sha") == _sha(body(srt.read(primary))):
                            continue
                    except OSError:
                        pass
            out.append(primary)
    return out


def main(name=None, season=None, episodes=None, redo=False, dry_run=False,
         revert_files=False, show=False):
    """Proofread every library episode that hasn't been (or all of them, with ``redo``)."""
    root = settings.SRT_TARGET_DIR or settings.EXPORTED_FILES_DIR
    todo = pending(root, name, season, episodes, redo=redo or revert_files or show)
    if not todo:
        raise UserError(
            f"nothing to proofread in {root}"
            + ("" if redo else " — every Primary is already proofread at the current rubric "
                               "(use --redo to do them again)"))

    if show:
        return _show(todo)
    if revert_files:
        note(f"reverting {len(todo)} episode(s)")
        return [p.name for p in todo if revert(p)]

    note(f"{len(todo)} episode(s) to proofread" + (" (--redo)" if redo else ""))
    for primary in todo:
        note(f"  {primary.parent.name}/{primary.name}")
    if dry_run:
        return _dry_run(todo)

    done, spent, fixed, glossed = [], 0.0, 0, []
    for number, primary in enumerate(todo, 1):
        note(f"\n=== {number}/{len(todo)}  {primary.name} ===")
        secondary = primary.with_name(primary.name.replace(" - Primary.srt", " - Secondary.srt"))
        stats = episode(primary, redo=redo)
        spent += stats["cost"]
        fixed += stats["changed"]
        if stats["why"]:
            note(f"  – {stats['why']}")
            continue
        done.append(primary.name)
        note(f"  ✓ {stats['changed']} fix(es), {stats['refused']} refused, "
             f"dictionary {stats['flagged_before']} → {stats['flagged_after']}")
        if stats["changed"] and secondary.exists():
            glossed.append(primary.name)

    note(f"\nProofread {len(done)} episode(s), {fixed} fix(es), ~${spent:.2f} notional")
    if glossed:
        # Named rather than acted on: correcting the German of an episode that already has a
        # Secondary leaves that Russian glossed from text no longer on disk, and `translate.py`
        # cannot notice — its work file keys on the filename and never hashes the German. The
        # user's decision of 2026-09-04 was to leave the Russian alone; saying which episodes
        # drifted is what keeps that a decision rather than a surprise.
        note(f"{len(glossed)} of them already had a Secondary, glossed from the German as it "
             f"was — `gigaku translate --redo` would re-do that Russian:")
        note("  " + ", ".join(glossed))
    return done


def _dry_run(todo):
    """What the dictionary doubts, per episode, with no model call and no cost."""
    for primary in todo:
        try:
            cues = srt.read(primary)
        except OSError as exc:
            note(f"  {primary.name}: {exc}")
            continue
        cache = _read_json(dict_path(primary), {})
        names = ledger(primary, _show_cues(primary), cache, ask=False)
        pool = candidates(_show_cues(primary), cache)
        flagged = unknown_words(sorted(_tokens(cues)), cache)
        _write_json(dict_path(primary), cache)
        note(f"\n{primary.name}: {len(cues)} cues, {len(flagged)} word(s) the dictionary "
             f"doesn't know, {len(pool)} recurring name(s)")
        if names:
            for line in ledger_lines(names):
                note(f"    {line}")
        note("  recurring: " + ", ".join(f"{w}×{n}" for w, n in
                                         sorted(pool.items(), key=lambda kv: -kv[1])[:24]))
        note("  unknown: " + ", ".join(flagged[:40]))
    return []


def _show(todo):
    """Every correction already made, and change nothing."""
    total = 0
    for primary in todo:
        record = journal(primary)
        edits = record.get("edits") or []
        refused = record.get("refused") or []
        if not edits and not refused:
            continue
        total += len(edits)
        note(f"\n{primary.parent.name}/{primary.name} — {len(edits)} fix(es), "
             f"dictionary {record.get('flagged_before')} → {record.get('flagged_after')}")
        for edit in edits:
            note(f"  {edit['cue']:>5}  {edit['before']}")
            note(f"         {edit['after']}")
        for bad in refused:
            note(f"  {bad['cue']:>5}  refused: {bad['why']}")
            note(f"         {bad['after']}")
    note(f"\n{total} correction(s) on record")
    return []
