"""Translate a ripped Primary track into the Secondary one, through Claude.

`gigaku subs` used to get its Russian from Language Reactor's machine translation, which is
the expensive and fragile half of a rip: LR fills its translation cache line by line, its
backend rate-limits a season (90s of backoff, up to eight times *per episode*), and a track
revert during that wait means it happily translates the wrong track while nothing stalls.
None of that buys quality — LR sees one line at a time, so a cue the exporter cut mid-clause
is translated as though it were a sentence.

This asks Opus instead, over the Claude Code subscription (`lib/claude.py`, the same
transport `gigaku clarity` runs on): no API key, no new dependency, and the model sees a
run of consecutive lines rather than one.

Three things this module is responsible for, none of which belong in the rubric:

* **Timecodes are never touched.** The Primary is read back off disk and only its text is
  replaced (`lib/subs/srt.py`), so the two tracks cannot drift apart by construction.
* **Nothing is asked for twice, and nothing is lost.** Every answered line is written to a
  work file the moment its request lands, so a rate limit costs one request rather than an
  episode — the lesson `lib/anki/score.py` records about long subscription runs.
* **An unanswered line is a named gap, never a hole in the file.** Lines that don't come
  back are re-asked with their neighbours as context; if they still don't come back the
  episode raises rather than writing a Secondary with blanks in it. The work file keeps
  everything already translated, so finishing it later costs only what is missing.
"""
import json
import os
import re
import tempfile
from pathlib import Path

from lib import claude
from lib.config import CACHE_DIR, UserError, note, settings
from lib.subs import library, srt, translate_prompt

WORK_DIR = os.path.join(CACHE_DIR, "subs-translate")
VERSION = 1


class TranslateError(Exception):
    """A translation that didn't complete. Recoverable: the Primary is untouched and the
    work file holds every line that did come back, so a re-run resumes at the gaps."""


def work_path(primary) -> Path:
    """Where an episode's answered lines are kept between requests.

    In the cache, not beside the SRTs: the library directory is *inside* the Chrome
    extension and `lib/subs/library.py` indexes what it finds there, so a stray file would
    be one more thing the extension has to know to ignore.
    """
    primary = Path(primary)
    return Path(WORK_DIR) / primary.parent.name / f"{primary.stem}.json"


def _read_work(path):
    """The lines already translated for this episode, if they are still comparable.

    A rubric edit discards them, deliberately. Keeping them would finish the episode half
    in one wording and half in another, and an episode is the unit a reader judges — one
    consistent translation is worth more than the handful of requests re-doing it costs.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    if data.get("fingerprint") != translate_prompt.FINGERPRINT:
        if data.get("lines"):
            note(f"    (rubric changed since {path.name} — re-translating the episode)")
        return {}
    lines = data.get("lines")
    if not isinstance(lines, dict):
        return {}
    return {int(k): v for k, v in lines.items() if str(k).isdigit() and v}


def _write_work(path, lines, model):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": VERSION,
            "fingerprint": translate_prompt.FINGERPRINT,
            "model": model,
            "lines": {str(k): lines[k] for k in sorted(lines)},
        },
        ensure_ascii=False,
        indent=1,
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".work-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _ask(view, model, retries, effort=None, known=()):
    """One request, plus up to ``retries`` re-asks for whatever didn't come back.

    A re-ask sends the *same* view with only the gaps marked (`regroup`), so the missing
    line is still translated with the scene around it — asking for it on its own would get
    back a sentence translated as if it stood alone, which for a cue cut mid-clause is
    exactly the wrong answer.
    """
    answers, spent, calls = {}, 0.0, 0
    for attempt in range(retries + 1):
        wanted = [cue.index for cue, needed in view if needed]
        if not wanted:
            break
        text, usage, cost = claude.ask(
            translate_prompt.render(view, known), translate_prompt.SYSTEM, model,
            what=f"{len(wanted)} subtitle lines", effort=effort,
        )
        spent += cost
        calls += 1
        got = translate_prompt.parse(text, wanted)
        answers.update(got)
        missing = [index for index in wanted if index not in answers]
        note(f"    …{len(answers)}/{len(answers) + len(missing)} lines"
             f" ({claude.describe(usage)})")
        if not missing:
            break
        if attempt < retries:
            note(f"    re-asking {len(missing)} line(s) that didn't come back")
            view = srt.regroup(view, missing)
    return answers, spent, calls


def _normalise(lines, conf):
    """Bring every joined token to its dictionary form. Returns (lines, cost, changed).

    **Why this is a pass and not another rubric rule.** Seven rubric edits took the defect from
    ~17 joined tokens per episode to ~6 and then stopped, and the way they stopped is the tell:
    «команда_игра», anchored in the rubric, came back right six times out of six, while
    «мяч_приёмка» — also anchored — came back right in one line and as «мяч_приёмки» in another
    *inside the same episode*. A rule applied to some occurrences and not others is not going
    to be fixed by stating it an eighth time; the model is glossing 350 lines and is spending
    its attention on meaning, which is where it belongs.

    So the check moves out of the rubric and gets its own call, over the only thing it needs to
    see. An episode holds ~30 distinct joined tokens; asked about those alone — no scene, no
    German, no word order to weigh — the job is a dictionary lookup. Measured on E14: one call,
    ~1k output tokens, ~$0.08, six tokens corrected, and the substitution afterwards is plain
    string replacement, so no timecode and no unjoined word can move.

    Best-effort by construction: a failure here returns the lines untouched with a note. The
    episode is glossed and on disk either way, and a missing normalisation is a handful of
    bent tokens, not a lost rip.
    """
    tokens = sorted({t for text in lines.values() for t in translate_prompt.JOINED.findall(text)})
    if not tokens:
        return lines, 0.0, 0
    try:
        reply, usage, cost = claude.ask(
            "\n".join(tokens), translate_prompt.NORMALISE, conf.SUBS_TRANSLATE_MODEL,
            what=f"{len(tokens)} gloss tokens", effort=conf.SUBS_TRANSLATE_EFFORT)
    except UserError as exc:
        note(f"  (tokens left as glossed — {exc})")
        return lines, 0.0, 0
    changes = translate_prompt.parse_normalise(reply, tokens)
    note(f"  dictionary form: {len(changes)} of {len(tokens)} token(s)"
         f" ({claude.describe(usage)})")
    if not changes:
        return lines, cost, 0
    # Longest first, so a token that is a prefix of another cannot be rewritten inside it.
    pattern = re.compile("|".join(re.escape(k) for k in sorted(changes, key=len, reverse=True)))
    return ({index: pattern.sub(lambda m: changes[m.group(0)], text)
             for index, text in lines.items()}, cost, len(changes))


def _unjoin(lines, conf):
    """Take the mark off joined tokens that are not a German word's parts. (lines, cost, n).

    `_normalise` fixes a part's *form*; this answers the prior question — whether the token
    should carry a mark at all. An underscore claims "these Russian words are the pieces of one
    German word", so «рот_на_замок» and «пускающий_пыль_в_глаза» are false claims, and taking
    the mark off removes the claim without touching a word.

    It is the one defect eleven rubric edits could not reach, and the reason is the same one
    that moved the dictionary check out of the rubric: told *not* to join a Russian phrase, the
    glosser obeys on most lines and not on all, because it is busy reading a scene. Asked about
    forty bare tokens with nothing else to weigh, the question is nearly a dictionary one.
    Measured across Blue Lock S01E12–24: 30 tokens split of 446, taking the season from 22
    phrase-joins to 2, and «под_брос_машины» — three parts of one real German compound — kept
    every time.

    Best-effort, like the pass before it, and one-directional by construction: a mark can be
    removed here, never added.
    """
    tokens = sorted({t for text in lines.values() for t in translate_prompt.JOINED.findall(text)})
    if not tokens:
        return lines, 0.0, 0
    try:
        reply, usage, cost = claude.ask(
            "\n".join(tokens), translate_prompt.UNJOIN, conf.SUBS_TRANSLATE_MODEL,
            what=f"{len(tokens)} gloss tokens", effort=conf.SUBS_TRANSLATE_EFFORT)
    except UserError as exc:
        note(f"  (marks left as glossed — {exc})")
        return lines, 0.0, 0
    split = translate_prompt.parse_unjoin(reply, tokens)
    note(f"  mark removed: {len(split)} of {len(tokens)} token(s)"
         f" ({claude.describe(usage)})")
    if not split:
        return lines, cost, 0
    pattern = re.compile("|".join(re.escape(t) for t in sorted(split, key=len, reverse=True)))
    return ({index: pattern.sub(lambda m: m.group(0).replace(translate_prompt.JOINER, " "), text)
             for index, text in lines.items()}, cost, len(split))


def episode(primary, secondary, *, conf=None, redo=False) -> dict:
    """Gloss one episode's Primary into ``secondary``. Returns a stats dict.

    ``redo`` throws away both the work file and any existing Secondary — that is the switch
    for re-glossing what Language Reactor machine-translated, rather than filling in what is
    missing.
    """
    conf = conf or settings
    cues = srt.read(primary)
    if not cues:
        raise TranslateError(f"{Path(primary).name} has no cues to translate")

    path = work_path(primary)
    lines = {} if redo else _read_work(path)
    cached = len(lines)
    todo = [cue for cue in cues if cue.index not in lines]
    spent, calls = 0.0, 0

    if todo:
        views = srt.groups(
            cues, conf.SUBS_TRANSLATE_GROUP, conf.SUBS_TRANSLATE_CONTEXT)
        # Already-answered lines become context rather than being asked for again: a resumed
        # episode re-reads them but never re-pays for them.
        views = [srt.regroup(view, [cue.index for cue, needed in view
                                                 if needed and cue.index not in lines])
                 for view in views]
        views = [view for view in views if any(needed for _, needed in view)]
        note(f"  translating {len(todo)} line(s) of {len(cues)} in {len(views)} request(s)"
             f" — {conf.SUBS_TRANSLATE_MODEL}"
             + (f", {cached} already done" if cached else ""))
        for number, view in enumerate(views, 1):
            note(f"  request {number}/{len(views)}")
            # Names are read off everything answered so far, cached lines included, so a
            # resumed episode inherits the spellings its earlier half settled on instead of
            # starting the cast again from the middle.
            answers, cost, made = _ask(view, conf.SUBS_TRANSLATE_MODEL,
                                       conf.SUBS_TRANSLATE_RETRIES,
                                       conf.SUBS_TRANSLATE_EFFORT,
                                       translate_prompt.names(lines))
            lines.update(answers)
            spent += cost
            calls += made
            _write_work(path, lines, conf.SUBS_TRANSLATE_MODEL)  # after every request

    missing = [cue.index for cue in cues if cue.index not in lines]
    if missing:
        shown = ", ".join(str(i) for i in missing[:10]) + ("…" if len(missing) > 10 else "")
        raise TranslateError(
            f"{len(missing)} of {len(cues)} lines never came back ({shown}) — "
            f"{len(lines)} translated line(s) are saved, re-run to finish the rest")

    # After the gaps are closed, never before: the pass is one call over the episode's distinct
    # joined tokens, so it has to see all of them. It runs on the way out rather than into the
    # work file, which keeps a resumed episode paying for it exactly once and keeps the cached
    # answers as the model actually gave them.
    # Unjoin first, then normalise: the second pass fixes the *form* of a token's parts, so it
    # should not spend its answer on tokens the first pass is about to take the mark off.
    fixed, changed = lines, 0
    if conf.SUBS_TRANSLATE_NORMALISE:
        for step in (_unjoin, _normalise):
            fixed, cost, count = step(fixed, conf)
            spent += cost
            changed += count

    srt.write(secondary, [srt.Cue(cue.index, cue.timecode, fixed[cue.index]) for cue in cues])
    return {"cues": len(cues), "translated": len(todo), "cached": cached,
            "calls": calls, "cost": spent, "normalised": changed}


# ── `gigaku translate` ───────────────────────────────────────────────────────

def pending(root, name=None, season=None, episodes=None, redo=False):
    """Episodes in the library that want a Secondary. Returns [(primary, secondary), …].

    The library is the source of truth rather than a list of what was ripped: a Primary is a
    Primary however it got there, so an episode `gigaku srt` converted by hand is finishable
    from here too.
    """
    root = Path(root)
    out = []
    for show in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        if name and name.lower() not in show.name.lower():
            continue
        for primary in sorted(show.glob("* - Primary.srt")):
            base = primary.name[: -len(" - Primary.srt")]
            title, base_season, episode = library.parse_base(base)
            if season is not None and (base_season or 0) != season:
                continue
            if episodes is not None and not episodes.contains(episode):
                continue
            secondary = primary.with_name(f"{base} - Secondary.srt")
            if secondary.exists() and not redo:
                continue
            out.append((primary, secondary))
    return out


def main(name=None, season=None, episodes=None, redo=False, dry_run=False):
    """Gloss every library episode that has no Secondary (or all of them, with ``redo``).

    One episode at a time, and a failure never stops the others: each one's lines are already
    saved, so the run that follows resumes rather than restarts.
    """
    root = settings.SRT_TARGET_DIR or settings.EXPORTED_FILES_DIR
    todo = pending(root, name, season, episodes, redo)
    if not todo:
        raise UserError(
            f"nothing to translate in {root}"
            + ("" if redo else " — every Primary already has a Secondary (use --redo to "
                               "re-gloss them)"))

    note(f"{len(todo)} episode(s) to gloss" + (" (--redo)" if redo else ""))
    for primary, _ in todo:
        note(f"  {primary.parent.name}/{primary.name}")
    if dry_run:
        return []

    done, failed, spent = [], [], 0.0
    for number, (primary, secondary) in enumerate(todo, 1):
        note(f"\n=== {number}/{len(todo)}  {primary.name} ===")
        # The German is proofread before it is glossed, here as in `subs._gloss` — an episode
        # reaching this command is usually one whose rip finished but whose gloss did not, so
        # this is where its Primary gets the pass it missed. Already-proofread episodes cost
        # nothing: `spell.episode` hashes the file and returns.
        if settings.SUBS_SPELL:
            from lib.subs import spell

            spell.episode(primary)
        try:
            stats = episode(primary, secondary, redo=redo)
        except (TranslateError, UserError) as exc:
            note(f"  ✗ {exc}")
            failed.append(primary.name)
            continue
        spent += stats["cost"]
        done.append(secondary.name)
        note(f"  ✓ {secondary.name} — {stats['translated']} line(s), "
             f"{stats['calls']} request(s)")
        # Per episode, not once at the end: an interrupted run still leaves everything
        # finished so far loadable by the extension, exactly as `gigaku subs` does.
        try:
            library.update(str(root))
        except OSError as exc:
            note(f"  (subtitle index not updated: {exc})")

    note(f"\nGlossed {len(done)} episode(s), ~${spent:.2f} notional")
    if failed:
        note(f"Unfinished ({len(failed)}): {', '.join(failed)} — re-run to resume")
    return done
