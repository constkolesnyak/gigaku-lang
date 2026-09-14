"""My Alternatives + My Sort Key + the my-learn tag, recomputed after every AnkiMorphs
recalc (was: study_morph_counter's field/tag half).

Reads every i+1 note's fields in bulk SQL (batched) and groups in Python — no per-note
``get_note`` and no per-morph search, so even a full pass over tens of thousands of cards
is a couple of seconds. ``get_note`` is called only for the notes that actually change.

Two silent wrongs the port fixed, both found by reading rather than by symptom: a note
whose cards sat in several decks was counted under whichever deck SQLite returned first
(now: the lowest card id's deck, deterministic), and a card in a filtered deck was counted
under the filtered deck rather than its home (`odid or did`). And the my-learn grouping is
now **by word alone** — the rule the tag's own docstring always promised ("exactly ONE
card per word") and the one lib/anki/select.learn_winners implements; the old `(deck,
word)` grouping could tag a word once per deck. Parity was measured on the live collection
before the change: zero picks moved (12 tags dropped, all ordinary post-recalc drift).
"""
from collections import defaultdict

from anki.collection import OpChanges
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp
from aqt.qt import QTimer

from .. import queries, rules
from ..core import addons, conf
from ..core.log import log
from ..core.patching import patch_once

_sweep_pending = False


def _cfg():
    section = conf.section("counter")
    section["ready_tag"] = conf.am_tags()["ready"]
    # A config from before the German note type has only the singular key.
    section["notetypes"] = list(section.get("notetypes") or [section["notetype"]])
    return section


def _fill(col, cfg, full):
    """Recompute counter + sort key for i+1 cards; on a full pass, maintain my-learn.

    One pass per note type, one update_notes at the end. Grouping (alternatives, the
    my-learn pick) is per note type on purpose: the note types are different languages,
    and a study morph can't name a word in two of them at once — the same script
    argument lib/vocab/ankimorphs.py's classifier rests on.
    """
    changed = {}
    for notetype in cfg["notetypes"]:
        _fill_one(col, cfg, notetype, full, changed)
    if not changed:
        return OpChanges()
    return col.update_notes(list(changed.values()))


def _fill_one(col, cfg, notetype, full, changed):
    counter_field, sort_field = cfg["counter_field"], cfg["sort_field"]
    learn_tag = cfg["learn_tag"]

    model = col.models.by_name(notetype)
    if not model:
        return
    order = {f["name"]: i for i, f in enumerate(model["flds"])}
    if any(cfg[k] not in order for k in ("counter_field", "sort_field", "study_field",
                                         "allcount_field")):
        return
    ci, si = order[counter_field], order[sort_field]
    mi, ai = order[cfg["study_field"]], order[cfg["allcount_field"]]
    # Optional: a collection that has never been scored has no such field, and the add-on
    # has to keep working there — so its absence falls back to the old three-part key.
    li = order.get(cfg["clarity_field"])
    sei = order.get(cfg["sentence_field"])

    all_query = queries.all_query(notetype, cfg["ready_tag"])
    ready_ids = col.find_notes(all_query)
    if not ready_ids:
        return

    # Bulk-read (flds, deck) for every i+1 note. Batched IN to stay under SQLite's
    # bound-parameter limit. Per note the row with the lowest card id wins, and the deck is
    # the card's *home* deck (odid while it sits in a filtered deck) — both deterministic.
    data = {}
    row_card = {}
    group = defaultdict(int)
    deck_name = {}
    for start in range(0, len(ready_ids), 400):
        chunk = ready_ids[start:start + 400]
        placeholders = ",".join("?" * len(chunk))
        rows = col.db.all(
            f"SELECT n.id, n.flds, c.did, c.odid, c.id FROM notes n JOIN cards c ON c.nid = n.id "
            f"WHERE n.id IN ({placeholders})", *chunk)
        for nid, flds, did, odid, cid in rows:
            if nid in row_card and row_card[nid] <= cid:
                continue
            row_card[nid] = cid
            home = odid or did
            parts = flds.split("\x1f")
            if home in deck_name:
                deck = deck_name[home]
            else:
                deck = deck_name[home] = col.decks.name(home)
            morph = parts[mi].strip() if mi < len(parts) else ""
            all_count = rules.to_int(parts[ai] if ai < len(parts) else "")
            clarity = (rules.to_clarity(parts[li]) if li is not None and li < len(parts) else 0)
            sentence = parts[sei] if sei is not None and sei < len(parts) else ""
            data[nid] = (morph, deck, all_count,
                         parts[ci] if ci < len(parts) else "",
                         parts[si] if si < len(parts) else "",
                         clarity, sentence)
    # Grouping keys are rules.fold()ed: Anki's search is case-insensitive, so L shows
    # Leben and leben as ONE word — counting them as two gave 67 German notes a wrong
    # My Alternatives and put them at the wrong place in the queue (measured 2026-08-08).
    for morph, deck, *_rest in data.values():
        if morph:
            group[(deck, rules.fold(morph))] += 1

    def _note(nid):
        if nid not in changed:
            changed[nid] = col.get_note(nid)
        return changed[nid]

    # Fields: recompute for all i+1 (full) or just those still missing a value (startup).
    missing_query = queries.missing_query(notetype, cfg["ready_tag"], counter_field, sort_field)
    targets = ready_ids if full else col.find_notes(missing_query)
    for nid in targets:
        rec = data.get(nid)
        if not rec:
            continue
        morph, deck, all_count, cur_counter, cur_sort, clarity, _sentence = rec
        if not morph:
            continue
        n = group[(deck, rules.fold(morph))]
        counter_value = str(n)
        sort_value = rules.sort_key(n, morph, clarity if li is not None else None, all_count)
        if cur_counter != counter_value or cur_sort != sort_value:
            note = _note(nid)
            note[counter_field] = counter_value
            note[sort_field] = sort_value

    # my-learn: only reconciled on a full (post-recalc) pass — it needs every i+1 card.
    # Exactly ONE card per word (the pick itself is rules.learn_pick, shared with the CLI);
    # words already being studied (any card in the Study deck) are skipped entirely.
    if full:
        learning = set()
        for nid in col.find_notes(queries.study_deck_query(notetype,
                                                           conf.study_deck_for(notetype))):
            note = col.get_note(nid)
            if cfg["study_field"] in note:
                word = rules.norm_morph(note[cfg["study_field"]])
                if word:
                    learning.add(rules.fold(word))

        # Folded keys here too — 16 German words carried TWO my-learn tags (Leben/leben)
        # because the "exactly ONE card per word" grouping and Anki's case-insensitive
        # search disagreed about what a word is. Self-heals on this very pass: one winner
        # per folded group, and the loser's tag is dropped by the reconciliation below.
        by_word = defaultdict(list)
        for nid, (morph, _deck, all_count, _c, _s, clarity, sentence) in data.items():
            word = rules.norm_morph(morph)
            if not word or rules.fold(word) in learning:
                continue
            by_word[rules.fold(word)].append((clarity, all_count, nid, sentence))

        should_learn = {rules.learn_pick(cards) for cards in by_word.values()}

        currently = set(col.find_notes(queries.learn_tag_query(notetype, learn_tag)))
        for nid in should_learn - currently:
            _note(nid).add_tag(learn_tag)
        for nid in currently - should_learn:
            _note(nid).remove_tag(learn_tag)


def run_fill(*, full):
    if mw is None or mw.col is None:
        return
    cfg = _cfg()
    if not full and not any(
            mw.col.find_notes(queries.missing_query(nt, cfg["ready_tag"],
                                                    cfg["counter_field"], cfg["sort_field"]))
            for nt in cfg["notetypes"]):
        return  # nothing to catch up on startup — don't flash a progress dialog
    CollectionOp(parent=mw, op=lambda col: _fill(col, cfg, full)).run_in_background()


# ── stale my-learn sweep (no recalc needed) ──────────────────────────────────


def _drop_stale_learn_tags(col, cfg):
    nids = [nid for nt in cfg["notetypes"]
            for nid in col.find_notes(queries.stale_learn_query(nt, cfg["learn_tag"],
                                                                cfg["ready_tag"]))]
    if not nids:
        return OpChanges()
    notes = []
    for nid in nids:
        note = col.get_note(nid)
        note.remove_tag(cfg["learn_tag"])
        notes.append(note)
    return col.update_notes(notes)


def _sweep_stale_now():
    """Drop my-learn from notes that stopped qualifying, without waiting for a recalc.

    The search runs *before* any CollectionOp is started, and returning early when it
    finds nothing is what stops this from looping: our own tag removal fires
    operation_did_execute again, the next sweep finds zero, and no further op is queued."""
    global _sweep_pending
    _sweep_pending = False
    if mw is None or mw.col is None:
        return
    cfg = _cfg()
    if not cfg.get("learn_tag"):
        return
    stale = [nid for nt in cfg["notetypes"]
             for nid in mw.col.find_notes(queries.stale_learn_query(nt, cfg["learn_tag"],
                                                                    cfg["ready_tag"]))]
    log(f"sweep fired: stale={len(stale)} nids={list(stale)[:5]}")
    if not stale:
        return
    CollectionOp(parent=mw, op=lambda col: _drop_stale_learn_tags(col, cfg)).run_in_background()


def sweep_stale_soon(delay_ms=1500):
    """Coalesce a burst into one sweep — marking a word known retags hundreds at once."""
    global _sweep_pending
    if _sweep_pending or mw is None:
        return
    _sweep_pending = True
    QTimer.singleShot(delay_ms, _sweep_stale_now)


def _on_operation_did_execute(*args):
    """``*args`` on purpose: aqt ships mypyc-compiled, so the hook's arity can't be read
    off the bundle, and it has changed across versions."""
    changes = args[0] if args else None
    if getattr(changes, "note", False) or getattr(changes, "tag", False):
        sweep_stale_soon()


# ── the recalc hook ──────────────────────────────────────────────────────────


def _patch_recalc(module):
    """Refill counters + my-learn after every recalc — via the shared ladder
    (`addons.when_recalc_ready`), which used to exist twice (W2, 2026-08-08)."""

    def wrap(original):
        def patched_on_success(_start_time):
            original(_start_time)
            # Defer so the recalc's own progress dialog has fully settled first.
            QTimer.singleShot(0, lambda: run_fill(full=True))

        return patched_on_success

    patch_once(module, "_on_success", wrap, "_gigaku_recalc_hook")


def _on_profile_open():
    # Catch up after all add-ons finish importing. The stale sweep is deliberately
    # separate from run_fill: that one returns early when no field is missing, which is
    # exactly a collection where only a tag went stale.
    QTimer.singleShot(1000, lambda: (run_fill(full=False), sweep_stale_soon(2000)))


def install():
    addons.when_recalc_ready(_patch_recalc)
    gui_hooks.profile_did_open.append(_on_profile_open)
    gui_hooks.operation_did_execute.append(_on_operation_did_execute)
