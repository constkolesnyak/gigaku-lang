"""`gigaku clarity` — fill My Clarity for i+1 cards, and the calibration that sizes the run.

What the score is for: the add-on gives each word's `my-learn` tag to its highest-clarity
card, so the one card you study for a word is the one whose sentence gives the meaning
away. Ranking the `L` alternates view is the secondary use.

The run is deliberately budgeted rather than all-at-once. Words are taken in study order
(biggest alternate group first, which is the order the queue is sorted by), so the words
whose best card gets identified first are the ones you are about to reach. A word already
fully cached doesn't consume budget, so a second run advances instead of re-walking the
head of the queue.

Both consumers of the score — the sort key (store.sort_key) and the `my-learn` tag
(`_retag`) — are applied here as well as in the add-on. The add-on stays authoritative: it
runs the same two rules over the whole collection after every AnkiMorphs recalc and would
overwrite any disagreement, so this is a second *writer*, not a second owner. It earns the
duplication because without it a scoring run is invisible — the tag is the queue you
actually study, and it would keep pointing at the old length-picked card until someone
remembered to press R.
"""
import os
import random
import time

from lib import config
from lib.anki import connect, prompt, score, select, store
from lib.config import UserError, note, settings

# Measured on this collection: ~5 minutes and ~130 output tokens per card. Scoring rides
# the Claude Code subscription, so what a run costs the user is *time* and rate-limit
# budget, not dollars — the CLI's own notional figure is reported after the fact.
SECONDS_PER_CARD = 1.5

CALIBRATE_CORRELATION_WORDS = 120
CALIBRATE_CORRELATION_PER_WORD = 6
# Words, not cards: the re-ask has to re-rate whole blocks or it cannot say whether the
# same card still wins. At CALIBRATE_CORRELATION_PER_WORD each that is about one group.
CALIBRATE_REPEAT_WORDS = 12


# ── loading ──────────────────────────────────────────────────────────────────────────

# The language profile every function below reads: collection facts, cache path, rubric
# and its fingerprint, bound once per invocation by `set_lang`. A module-level namespace
# (like RUN below) rather than a parameter through fifteen signatures.
P = config.clarity_profile("ja")


def set_lang(lang):
    """Bind this run to one language's profile (2026-08-07 — German scores too).

    Everything moves together — notetype, study deck, cache file, rubric, fingerprint —
    because splitting any pair is a measured disaster: a shared cache false-restales the
    other language via `store.last_rubric(P.cache)`, and a rubric judged against the wrong
    corpus is the hand-written-anchors mistake again. The German rubric (prompt_de.py)
    carries provisional anchors from real cards; re-anchor after the first `--calibrate`.
    """
    global P
    P = config.clarity_profile(lang)


def _field_names():
    return connect.model_field_names(P.notetype)


# The fields this command owns, and the description each carries in Anki.
def _owned_fields():
    return (
        (P.field, config.CLARITY_FIELD_DESCRIPTION),
        (P.run_field, config.CLARITY_RUN_FIELD_DESCRIPTION),
    )


def ensure_field():
    have = _field_names()
    missing = [name for name, _ in _owned_fields() if name not in have]
    if not missing:
        return
    raise UserError(
        f'the note type "{P.notetype}" is missing {", ".join(missing)}. '
        f"Run `gigaku clarity --add-field` once to create them."
    )


def add_field(*, assume_yes=False):
    """Create the fields this command owns. Separate because it forces a full upload."""
    have = _field_names()
    missing = [(n, d) for n, d in _owned_fields() if n not in have]
    if not missing:
        note("all fields already exist — nothing to do.")
        return
    note(
        f'This adds {", ".join(n for n, _ in missing)} to "{P.notetype}".\n'
        f"Adding a field bumps the collection schema, so Anki will ask for a ONE-WAY SYNC\n"
        f"and re-upload the collection (~150 MB — the collection only, not the media)."
    )
    if not assume_yes and not _confirm("Add them?"):
        raise UserError("cancelled")
    for name, description in missing:
        connect.add_field(P.notetype, name, description)
        note(f'  added "{name}"')
    note("sync Anki (choose upload) when it asks.")


def reset(*, assume_yes=False):
    """Clear every My Clarity value and the score cache — judge the collection again.

    Wanted whenever the rubric or the scale changes: old scores and new ones inside the
    same word are worse than no scores at all, because the pick compares them directly.
    Nothing else is touched — my-learn simply falls back to am-all-morphs-count until the
    next run, which is exactly where it sat before any of this existed.
    """
    ensure_field()
    # Scoped to the profile's notetype like every other query here: My Clarity is the
    # same field name in both languages, so an unscoped find under --lang de would clear
    # all 15,432 Japanese scores while deleting only the German cache.
    ids = connect.find_notes(f'note:"{P.notetype}" "{P.field}:_*"')
    cached = len(store.read(P.cache))
    note(f"this clears {P.field} on {len(ids):,} notes and drops "
         f"{cached:,} cached scores. Re-scoring them costs a full run.")
    if not assume_yes and not _confirm("Wipe?"):
        raise UserError("cancelled")
    if ids:
        connect.update_fields({nid: {P.field: ""} for nid in ids})
    try:
        os.unlink(P.cache)
    except FileNotFoundError:
        pass
    note("cleared. Press R in Anki — my-learn goes back to the sentence-length rule.")


def load_cards():
    query = f'note:"{P.notetype}" tag:"{P.ready_tag}"'
    note_ids = connect.find_notes(query)
    if not note_ids:
        raise UserError(f"no notes match {query} — is this the right collection?")
    note(f"reading {len(note_ids):,} i+1 notes from Anki…")
    records = connect.notes_info(note_ids)
    return select.from_notes(
        records,
        sentence_field=P.sentence_field,
        morph_field=P.morph_field,
        allcount_field=P.allcount_field,
        clarity_field=P.field,
        alternates_field=P.alternates_field,
        sort_field=P.sort_field,
        run_field=P.run_field,
    )


def _confirm(question):
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ── estimating ───────────────────────────────────────────────────────────────────────

def _announce(cards, groups, what):
    minutes = len(cards) * SECONDS_PER_CARD / 60
    note(f"{what}: {len(cards):,} cards in {len(groups):,} `claude` calls "
         f"— roughly {minutes:.0f} min on the subscription")


def _run(cards, cache, *, label="scoring"):
    """Score `cards`, saving after every group. Returns ({note_id: 0..SCALE}, cache, $).

    Persisting per group rather than at the end is the difference between a rate limit
    costing you the last few minutes and costing you the whole run — and on a job measured
    in hours, meeting one is expected rather than exceptional.
    """
    _ensure_run()   # first actual scoring claims the number — see _ensure_run
    by_id = {c.note_id: c for c in cards}
    items = _items(cards)
    groups = prompt.groups(items, settings.CLARITY_GROUP)
    state = {"cache": cache, "written": 0}

    def landed(index, total, group_items, got, usage, spent):
        done = [by_id[i[0]] for i in group_items if i[0] in got]
        state["cache"], written = _persist(done, got, state["cache"])
        state["written"] += written
        note(f"  {label} {index}/{total}: {len(got)}/{len(group_items)} scored, "
             f"{written} written ({score.describe(usage)}, ~${spent:.2f} notional)")

    scores, spent = score.run(groups, settings, system=P.rubric, on_group=landed)

    missing = [by_id[i[0]] for i in items if i[0] not in scores]
    if missing:
        # A group can come back short, or with an id we never asked for. One re-ask, then
        # the stragglers are left unscored rather than guessed at.
        #
        # **Whole words, not the orphans.** Re-asking three cards out of a word's twelve
        # rates them against each other and nothing else, and those numbers are then
        # compared — in the sort key and in the my-learn pick — with the ten that came back
        # from the full block. That is the same two-half-comparisons failure `prompt.groups`
        # exists to prevent, arriving through the back door. Re-scoring the whole word costs
        # a few extra cards and keeps every number inside a word from one comparison.
        words = {c.morph for c in missing}
        redo = [c for c in cards if c.morph in words]
        note(f"  {len(missing)} cards came back unscored — re-asking their "
             f"{len(words)} word(s) in full ({len(redo)} cards)")
        again, more = score.run(
            prompt.groups(_items(redo), settings.CLARITY_GROUP), settings,
            system=P.rubric, on_group=landed,
        )
        scores.update(again)
        spent += more
    return scores, state["cache"], spent


def _retag(cards, cache):
    """Move `my-learn` onto each word's clearest card, now rather than at the next recalc.

    The add-on stays authoritative — it runs this same rule over the whole collection after
    every AnkiMorphs recalc and would overwrite any disagreement. Doing it here as well is
    what makes a scoring run visible immediately: the tag is the queue you actually study,
    and leaving it pointing at the old length-picked card until you remember to press R
    hides the entire point of the score.
    """
    studying = set()
    for record in connect.notes_info(
        connect.find_notes(f'note:"{P.notetype}" '
                           f'deck:"{P.study_deck}"')
    ):
        word = select.norm_morph(
            (record.get("fields", {}).get(P.morph_field) or {}).get("value", "")
        )
        if word:
            studying.add(select.fold(word))  # learn_winners keys on folded words

    should = select.learn_winners(cards, cache, studying=studying)
    current = set(connect.find_notes(
        f'note:"{P.notetype}" tag:"{P.learn_tag}"'))
    add, drop = should - current, current - should
    if add:
        connect.add_tags(sorted(add), P.learn_tag)
    if drop:
        connect.remove_tags(sorted(drop), P.learn_tag)
    if add or drop:
        note(f"my-learn: moved onto {len(add):,} cards, off {len(drop):,}")
    return len(add), len(drop)


def _items(cards):
    return [(c.note_id, c.sentence, c.morph) for c in cards]


RUN = {"number": None}  # set once per invocation by main()/calibrate()


def _rubric_notice():
    """Say it out loud when the wording moved since the last run.

    A rubric edit invalidates nothing by itself — scored words are skipped, so old and new
    numbers never meet unless a word is re-scored. But when they do meet they meet *inside
    one word*, which is the only place they are ever compared, so this is worth a line
    rather than being noticed later in a shuffled alternates view.
    """
    previous = store.last_rubric(P.cache)
    if previous and previous != P.fingerprint:
        note(f"the rubric changed ({previous} → {P.fingerprint}). Scored words are "
             f"skipped, so nothing mixes on its own; `--restale` re-judges them whole, "
             f"`--reset` throws everything away.")


def _ensure_run():
    """Claim a run number, the first time there is actually something to stamp.

    Lazily, and that is the point: claiming it up front burns a number on a pass that turns
    out to have no work — which is exactly what run #5 in the log is, a number with no card
    carrying it. The number means "these cards were judged by this model under this
    rubric", so it should not exist until a card has been.
    """
    if RUN["number"] is None:
        RUN["number"] = store.start_run(settings.CLARITY_MODEL, P.fingerprint, path=P.cache)
        note(f"run #{RUN['number']} ({settings.CLARITY_MODEL}, "
             f"rubric {P.fingerprint})")


def _persist(cards, by_note_id, cache):
    """Fold new scores into the cache and push the changed fields into Anki.

    The rubric is stamped here, beside the score and in the same atomic write, so a card's
    wording is recorded by the pass that produced it rather than inferred later from a
    field that a re-score is about to clear.
    """
    keyed = {c.key: by_note_id[c.note_id] for c in cards if c.note_id in by_note_id}
    cache = store.merge(cache, keyed)
    rubrics = store.read_rubrics(P.cache)
    rubrics.update(dict.fromkeys(keyed, P.fingerprint))
    store.write(cache, path=P.cache, rubrics=rubrics)

    changes = store.updates(cards, cache, clarity_field=P.field,
                            sort_field=P.sort_field,
                            run_field=P.run_field, run=RUN["number"])
    if changes:
        connect.update_fields(changes)
    return cache, len(changes)


# ── the normal run ───────────────────────────────────────────────────────────────────

def _reconcile(cards, cache):
    """Repair anything the collection and the cache disagree about, before scoring.

    Three separate drifts, all cheap because only differences are written:
    a run that died before writing its fields, a cleared field, and — the one that
    actually breaks behaviour — sort keys left in the old three-part format, which cannot
    be compared against four-part ones and shuffle the alternates view into nonsense.
    """
    stale = store.updates(cards, cache, clarity_field=P.field,
                          sort_field=P.sort_field)
    if stale:
        note(f"  reconciling {len(stale):,} scored cards whose fields drifted")
        connect.update_fields(stale)

    keys = store.sort_key_updates(cards, cache, sort_field=P.sort_field)
    if keys:
        note(f"  normalising {len(keys):,} sort keys")
        connect.update_fields(keys)


def _min_morphs():
    """The floor for THIS run's language. German measured its own (two --calibrate
    runs, 2026-08-08): its corpus plateaus at mean ~0.2 past 7 morphs and the knee
    where fragments end is 5 — the ja 9 would drop most of the scoreable German
    cards. The --min-morphs / --min-morphs-de flags and their GIGAKU_* envs override
    per language, so an A/B never has to edit code."""
    return (settings.CLARITY_MIN_MORPHS_DE if P.lang == "de"
            else settings.CLARITY_MIN_MORPHS)


def _scope(cards, cache):
    """(cards this settings profile would ever score, how many are done)."""
    everything = select.scoreable(
        cards,
        min_morphs=_min_morphs(),
        per_word=settings.CLARITY_PER_WORD,
        ceiling=settings.CLARITY_GROUP,
    )
    return everything, sum(1 for c in everything if c.key in cache)


def _known(cache, *, restale):
    """The keys this pass will treat as already answered.

    With `--restale` the ones stamped with another rubric are dropped from that set, which
    puts their **words** — whole, since `select.select` never sends half of one — back at
    the head of the queue. Nothing is cleared first: a word is scored in one call and
    written the moment it lands, so an interrupted run leaves old numbers standing rather
    than a hole. (The scripted version of this cleared the fields up front to select from,
    which is how it managed to empty 2,855 cards and score none of them.)
    """
    known = set(cache)
    if not restale:
        return known
    gone = store.stale(cache, store.read_rubrics(P.cache), P.fingerprint)
    if gone:
        note(f"--restale: {len(gone):,} cached scores came from another rubric "
             f"(current: {P.fingerprint}) — their words go back in the queue")
    return known - gone


def _batch(cache, *, assume_yes, dry_run, restale=False):
    """One budgeted pass: select, score, write, move the tag. Returns cards scored.

    Reloads the collection every time on purpose. A pass takes many minutes, and over
    that span cards leave i+1 as words are learnt — scoring against a stale snapshot
    spends the budget on cards that are no longer in the queue.
    """
    cards = load_cards()
    _reconcile(cards, cache)
    known = _known(cache, restale=restale)
    everything, done = _scope(cards, known)
    cap = ("every alternate" if settings.CLARITY_PER_WORD <= 0
           else f"top {settings.CLARITY_PER_WORD} per word")
    note(f"{len(cards):,} i+1 cards · {len(everything):,} in scope "
         f"(≥{_min_morphs()} morphs, {cap}) · {done:,} already scored")

    chosen, words = select.select(
        cards,
        min_morphs=_min_morphs(),
        per_word=settings.CLARITY_PER_WORD,
        words=settings.CLARITY_WORDS,
        scored=known,
        ceiling=settings.CLARITY_GROUP,
    )
    if not chosen:
        note("nothing left to score in this budget.")
        _retag(cards, cache)
        return 0, cache

    score.executable()  # fail before the confirmation, not after it
    _announce(chosen, prompt.groups(_items(chosen), settings.CLARITY_GROUP),
              f"about to score {words:,} words")
    if dry_run:
        return 0, cache
    if not assume_yes and not _confirm("Go?"):
        raise UserError("cancelled")

    scored, cache, spent = _run(chosen, cache)
    note(f"scored {len(scored):,} cards (~${spent:.2f} notional).")
    # Re-read: scoring changed the fields under our snapshot, and the tag rule reads them.
    _retag(load_cards(), cache)
    return len(scored), cache


def main(*, assume_yes=False, dry_run=False, loop=False, restale=False):
    """Score one budget's worth of words, or keep going until the scope is empty.

    `loop` exists because the whole job is hours and a single pass is minutes: without it
    the collection is only ever partly judged, and driving the repetition from a shell
    script (which is how this started) puts the retry policy and the stop condition
    somewhere they cannot be tested. A failed pass is almost always a rate limit or a
    `claude` hiccup, so it backs off and tries again rather than surrendering the run.
    """
    ensure_field()
    _rubric_notice()

    cache, total, failures, passes = store.read(P.cache), 0, 0, 0
    while True:
        passes += 1
        if loop:
            note(f"── pass {passes} ─────────────────────────────")
        try:
            scored, cache = _batch(cache, assume_yes=assume_yes, dry_run=dry_run,
                                   restale=restale)
            failures = 0
        except UserError as exc:
            if not loop or "cancelled" in str(exc):
                raise
            failures += 1
            if failures >= settings.CLARITY_RETRIES:
                note(f"giving up after {failures} passes in a row failed: {exc}")
                break
            note(f"pass failed ({exc}) — waiting {settings.CLARITY_BACKOFF}s, "
                 f"attempt {failures}/{settings.CLARITY_RETRIES}")
            time.sleep(settings.CLARITY_BACKOFF)
            continue

        total += scored
        if not loop or scored == 0:
            break

    if loop and not dry_run:
        note(f"done: {total:,} cards over {passes} pass(es) as run #{RUN['number']}.")


# ── calibration ──────────────────────────────────────────────────────────────────────

def calibrate(*, assume_yes=False, dry_run=False):
    """Answer three questions in one batch: what threshold, is it real, is it stable."""
    ensure_field()
    cards = load_cards()
    cache = store.read(P.cache)
    _rubric_notice()

    sample = select.stratified(cards, settings.CLARITY_SAMPLE)
    pairs_sample = _correlation_sample(cards)
    # One scoring pass over the union — the two samples overlap, and a card is a card.
    merged = {c.note_id: c for c in sample + pairs_sample}
    chosen = [c for c in merged.values() if c.key not in cache]

    if chosen:
        score.executable()
        _announce(chosen, prompt.groups(_items(chosen), settings.CLARITY_GROUP),
                  "calibration")
        if dry_run:
            return
        if not assume_yes and not _confirm("Go?"):
            raise UserError("cancelled")
        _, cache, _ = _run(chosen, cache, label="calibrating")
    elif dry_run:
        return

    fractions = store.as_fraction(cache, list(merged.values()))

    print("morphs\tsampled\tmean_clarity\tshare_ge_0.7\tcards\tcards_at_or_above")
    for row in select.bucket_rows(cards, fractions):
        mean = "-" if row["mean"] is None else f"{row['mean']:.2f}"
        share = "-" if row["share_high"] is None else f"{row['share_high']:.2f}"
        print(f"{row['morphs']}\t{row['sampled']}\t{mean}\t{share}\t"
              f"{row['cards']}\t{row['cards_at_or_above']}")

    threshold = select.recommend(select.bucket_rows(cards, fractions))
    if threshold is None:
        note("\nno bucket cleared the yield floor — the sentences may be too short for "
             "this to be worth doing at all.")
    else:
        env = ("GIGAKU_CLARITY_MIN_MORPHS_DE" if P.lang == "de"
               else "GIGAKU_CLARITY_MIN_MORPHS")
        note(f"\nsuggested threshold: {env}={threshold}")

    _report_correlation(pairs_sample, fractions)
    if not dry_run:
        _report_repeatability(cache, list(merged.values()))


def _correlation_sample(cards):
    """Whole words (top-K alternates each), so clarity and the proxy can be ranked head
    to head *within* a word — which is the only comparison the L view actually makes."""
    groups = select.by_word(cards)
    rich = sorted(
        (m for m, g in groups.items() if len(g) >= 5),
        key=lambda m: (-len(groups[m]), m),
    )
    rng = random.Random(0)
    picked = rich[:CALIBRATE_CORRELATION_WORDS * 4]
    rng.shuffle(picked)
    out = []
    for morph in picked[:CALIBRATE_CORRELATION_WORDS]:
        out.extend(select.candidates(
            groups[morph], min_morphs=0, per_word=CALIBRATE_CORRELATION_PER_WORD
        ))
    return out


def _report_correlation(sample, fractions):
    """Does clarity just reproduce am-all-morphs-count? If so, none of this is worth it."""
    values = []
    for group in select.by_word(sample).values():
        scored = [c for c in group if c.note_id in fractions]
        if len(scored) < 3:
            continue
        rho = select.spearman(
            [(c.all_count, fractions[c.note_id]) for c in scored]
        )
        if rho is not None:
            values.append(rho)
    if not values:
        note("correlation check: not enough scored alternates to say.")
        return
    mean = sum(values) / len(values)
    note(f"\nwithin-word rank correlation with am-all-morphs-count: "
         f"mean rho {mean:+.2f} over {len(values)} words")
    if mean > 0.85:
        note("  ! clarity is tracking the morph count almost exactly — the collection "
             "already sorts alternates this way, so this buys close to nothing.")
    elif mean < 0.3:
        note("  clarity is largely independent of the proxy — this is new information.")


def _report_repeatability(cache, cards):
    """Re-ask whole words. **What has to be stable is the pick, not the number.**

    Two things this used to get wrong. It re-asked a scatter of individual cards, so it
    could report how much a *number* moved but never whether the same card still won —
    which is the only thing the score decides. And it printed "points (of 10)" with a
    warning above 2, both left over from the old 0..10 scale: on 0..100 the label was wrong
    by a factor of ten and the warning fired on every run, which is the same as no warning.
    """
    scored = [c for c in cards if c.key in cache]
    groups = {w: g for w, g in select.by_word(scored).items() if len(g) >= 2}
    if not groups:
        return
    rng = random.Random(1)
    words = sorted(groups)
    rng.shuffle(words)
    words = words[:CALIBRATE_REPEAT_WORDS]
    repeat = [c for w in words for c in groups[w]]
    again, _ = score.run(
        prompt.groups(_items(repeat), settings.CLARITY_GROUP), settings, system=P.rubric
    )
    diffs = [abs(again[c.note_id] - cache[c.key]) for c in repeat if c.note_id in again]
    if not diffs:
        return

    def winner(group, values):
        return max(group, key=lambda c: (values(c), c.all_count, -c.note_id)).note_id

    kept, compared = 0, 0
    for word in words:
        group = [c for c in groups[word] if c.note_id in again]
        if len(group) < 2:
            continue
        compared += 1
        kept += (winner(group, lambda c: cache[c.key])
                 == winner(group, lambda c: again[c.note_id]))

    mean = sum(diffs) / len(diffs)
    note(f"\nrepeatability over {len(diffs)} re-asked cards in {compared} words: "
         f"{sum(1 for d in diffs if d == 0)/len(diffs):.0%} identical, "
         f"mean |Δ| {mean:.1f} points (of {prompt.SCALE})")
    if compared:
        note(f"  same winner for {kept}/{compared} words ({kept/compared:.0%}) — this is "
             f"the number that matters: it is what my-learn keeps or moves.")
    if compared and kept / compared < 0.8:
        note("  ! the pick is unstable — a re-score would reshuffle which card you study.")
