"""The browse keys — move, known-toggle, flag+translate, trim, ask (was browser_jk_nav's
key half).

**Which key is which is not written here.** Every one is a config value, in gigaku's own
config (Tools ▸ Add-ons ▸ gigaku ▸ Config) and nowhere else — `known_toggle_hotkey` is `M`
because `I` belongs to MvJ's View Context. Reading MvJ's Hotkeys tab for these was tried
and taken back out: a filled row there is what makes *MvJ* bind its own version of an
action, so a row naming a key this side already held put two claims on it, and Qt answers
two claims by firing neither — see the note above `conf.mvj_hotkey`.

Delivery is three event filters (card list, sidebar, **and the Browser window itself**)
plus QShortcuts on the table view. One Qt rule makes that sound: a key reaches the focused
widget and only propagates up the parent chain if that widget *ignores* it — so the
window-level filter sees exactly the keys nobody consumed, and never the ones typed into
the search bar, a tag box or an editor field. Without it, anything that moved focus off
the card list left the keys dead until a row was clicked — the trimmer, for one, reloads
the note into the editor as it closes, so `u` worked once and then silently did nothing.
The filter only carries **bare single keys**, which is all it can carry: it matches one key
code against an unmodified press. A hotkey with modifiers in it is left to its QShortcut,
which handles sequences natively — so `Ctrl+M` works, it just doesn't reach the sidebar.
"""
from collections import defaultdict

from aqt import gui_hooks, mw
from aqt.browser import Browser
from aqt.qt import QAction, QEvent, QKeySequence, QObject, QShortcut, Qt, QTimer
from aqt.utils import tooltip

from ..core import addons, conf
from ..core import browser as b
from ..core.browser import focus_card_list
from ..core.log import log
from ..core.patching import patch_once

# action -> the key in this add-on's `keys` config section that sets it.
_ACTIONS = {
    "row_down": "row_down_hotkey",
    "row_up": "row_up_hotkey",
    "known": "known_toggle_hotkey",
    "flag": "flag_translate_hotkey",
    "trim": "trim_audio_hotkey",
    "ask": "ask_question_hotkey",
}


def _hotkeys():
    """action -> the key it is on now, empty ones dropped (an empty key switches it off).

    **One source, and it is this add-on's own config.** Reading MvJ's Hotkeys tab as well
    was tried across an evening and taken back out: those rows are what make *MvJ* bind its
    own version of an action, so every row that named a key this side already held produced
    two claims on it — and Qt answers two claims by firing neither. That is what "View
    Context doesn't work" and "the shortcuts are slow and glitchy" both were. One key, one
    binding, one place to change it."""
    cfg = conf.section("keys")
    resolved = {}
    for action, config_key in _ACTIONS.items():
        key = str(cfg.get(config_key) or "").strip()
        if key:
            resolved[action] = key
    return resolved


def _bare_key(hotkey):
    """The single unmodified key of `hotkey`, or None if it carries modifiers.

    Only a bare key can be matched by the event filter, which compares one key code against
    a press with no modifiers; anything else is the QShortcut's job."""
    try:
        sequence = QKeySequence(hotkey)
        if sequence.count() != 1:
            return None
        combination = sequence[0]
        if combination.keyboardModifiers() != Qt.KeyboardModifier.NoModifier:
            return None
        return combination.key()
    except Exception as exc:  # noqa: BLE001 — an odd binding must not cost the others
        log(f"keys: {hotkey!r} is not a plain key ({exc!r})")
        return None


def _study_field():
    return conf.section("counter")["study_field"]


def _learn_tag():
    """The my-learn tag, or "" when it is switched off.

    Owned by `features/counter.py`, which is the only thing that ever *awards* it; this
    module removes it with the queue tags and puts it back on an undo, which is the same
    tag following the same word out of the queue and back in."""
    return str(conf.section("counter").get("learn_tag") or "").strip()


def _leave_queue(note, tags, learn_tag):
    """Take a note out of the i+1 queue: AnkiMorphs' status tags **and my-learn**.

    my-learn *is* the queue — J's view is `tag:my-learn` — so a word marked known that
    keeps the tag stays sitting in the very view it just left, which is what the user saw.

    counter.py's stale sweep was meant to catch exactly this and structurally cannot. It
    rides on `gui_hooks.operation_did_execute`, and that hook has precisely two callers in
    the shipped aqt (read off the bytecode, 26.5): `operations.on_op_finished` — the
    CollectionOp machinery — and `AnkiQt._synthesize_op_did_execute_from_reset`, the
    legacy `mw.reset()` path. Every mark on this side writes with a raw
    `col.update_notes`, which is neither, so the sweep simply never ran after a press; it
    fired only when some *other* op happened to come along later. Dropping the tag in the
    same write as the status tags makes the press and its effect one event. The sweep
    stays for the other way out of i+1 — a card recalc bumps to i+≥2 — which no keypress
    can see."""
    for tag in (tags["ready"], tags["not_ready"]):
        if note.has_tag(tag):
            note.remove_tag(tag)
    if learn_tag and note.has_tag(learn_tag):
        note.remove_tag(learn_tag)


def _being_studied(word, notetype, study_field):
    """Whether `word` already has a card in its language's Study deck.

    counter.py skips such a word when it awards my-learn — it is being learned, so it has
    no business in the queue — and the undo has to agree with it or it would hand the tag
    to a word that never held one. Asked per word (`alt_query` again, the one definition
    of "this word's cards"), not by walking the whole Study deck: an undo touches one word
    and can afford one search."""
    from .. import queries

    try:
        return bool(mw.col.find_notes(
            f'{queries.alt_query(notetype, study_field, word)} '
            f'deck:"{queries.search_escape(conf.study_deck_for(notetype))}"'))
    except Exception as exc:  # noqa: BLE001 — an unreadable deck must not cost the undo
        log(f"studying {word!r}: FAILED {exc!r}")
        return False


def _land_near(browser, anchor):
    """Re-search, and put the cursor back where the press happened — not on the top row.

    A mark takes its word out of the my-learn view, so the selected row stops existing and
    Anki's own `_restore_selection` has nothing to restore; the view then opens at row 0
    and the next press acts on the first word of the queue instead of the next one. The
    landing machinery already answers exactly this for J/L/O (`b.capture_anchor` records
    the current row and then its neighbours, **downwards first** — the row that took the
    marked word's place is the next word to study, the row above is the one already done),
    and it only ever needed asking for here.

    The anchor is captured before the write, because it has to describe the view the user
    was looking at. With nothing captured the selection is simply left alone rather than
    sent to the top."""
    if anchor:
        b.land_after(browser, b.select_anchor(anchor))
    try:
        browser.search()
    except Exception as exc:  # noqa: BLE001 — the tags are written; the view is cosmetic
        log(f"land: re-search FAILED {exc!r}")


def _restore_learn_tags(notes, study_field, ready_tag, learn_tag):
    """Put my-learn back on one card per word — the undo half of `_leave_queue`.

    The tag is *derived*, one card per word, so an undo cannot simply put back what it
    took: it has to re-run the pick (`rules.learn_pick`, the same one counter.py makes
    after a recalc) over the cards that just came back into the queue. Without it,
    undoing a mark would leave the word out of J's view until the next R — an undo that
    does not undo. Only cards that got the ready tag back can win it: my-learn marks one
    i+1 card, and an i+≥2 card is not in the queue at all.

    A word with a card already in the Study deck is skipped, exactly as counter.py skips
    it: such a word is being learned, so it never held the tag to begin with, and putting
    one on it would not restore anything — it would invent it."""
    from .. import rules

    if not learn_tag:
        return 0
    cfg = conf.section("counter")
    by_word = defaultdict(list)
    named = {}
    for note in notes:
        if not note.has_tag(ready_tag):
            continue
        word = rules.norm_morph(note[study_field].strip() if study_field in note else "")
        if not word:
            continue
        key = rules.fold(word)
        # Folded, like counter.py's grouping: Leben and leben are one word to Anki's
        # case-insensitive search, so they must be one word to the pick as well.
        named.setdefault(key, (word, _own_notetype(note)))
        by_word[key].append((
            rules.to_clarity(note[cfg["clarity_field"]])
            if cfg["clarity_field"] in note else 0,
            rules.to_int(note[cfg["allcount_field"]])
            if cfg["allcount_field"] in note else 0,
            note.id,
            note[cfg["sentence_field"]] if cfg["sentence_field"] in note else "",
        ))
    picked = set()
    for key, cards in by_word.items():
        # One search per distinct word, not per note: an undo of a 200-card word must
        # not ask the same question 200 times.
        if _being_studied(*named[key], study_field):
            continue
        picked.add(rules.learn_pick(cards))
    for note in notes:
        if note.id in picked and not note.has_tag(learn_tag):
            note.add_tag(learn_tag)
    return len(picked)


def _stepper(browser, move):
    def step():
        table = browser.table
        if not table.len():
            return
        # A fresh search can leave the table with no current row, and then j/k has
        # nowhere to move from; land on the first row instead.
        if table.has_current():
            move()
        else:
            table.to_first_row()

    return step


def _known_group(card_ids, study_field, known_m, known_a):
    """note id -> note, for every card of the selected cards' words still marked known.

    **Matched the way the word is marked — which is precise now.** The substring shape
    (`am-study-morphs:*数*`) was right when the thing being undone was MvJ's own sweep:
    an undo must reverse the operation, and that operation swept 数字, 数学 and 数える
    along (424 notes, 143 left marked, measured). But the operation died with
    `mvj_retunes._precise_tag_as_known` — every mark, key or MvJ button, now goes through
    `queries.alt_query` — so an undo broader than any mark it could be reversing doesn't
    reverse, it fabricates. German made the gap catastrophic where Japanese merely
    over-swept compounds: by substring, `sein` gathers 1,499 notes of which 327 are the
    word, and `er` gathers 1,488 of which 0 are (measured 2026-08-08).

    The query is alt_query — the exact field shapes, scoped to the pressed card's own
    notetype, case-insensitive by Anki itself — restricted to the known tags: exactly
    the set a precise mark took, in reverse. A card carrying a known tag *and* a
    non-empty study field is the contradictory state a mark leaves behind, which is why
    an empty field is never used to gather."""
    from .. import queries, rules

    group = {}
    seen = set()
    for card_id in card_ids:
        note = mw.col.get_card(card_id).note()
        study = note[study_field].strip() if study_field in note else ""
        if not (note.has_tag(known_m) or (note.has_tag(known_a) and study)):
            continue
        group[note.id] = note
        word = rules.norm_morph(study)
        notetype = _own_notetype(note)
        if not word or (rules.fold(word), notetype) in seen:
            continue
        seen.add((rules.fold(word), notetype))
        query = (f'{queries.alt_query(notetype, study_field, word)} '
                 f'(tag:"{known_a}" OR tag:"{known_m}")')
        try:
            found = mw.col.find_notes(query)
        except Exception as exc:  # noqa: BLE001 — one bad morph must not sink the rest
            log(f"undo known: {query!r} FAILED {exc!r}")
            continue
        for note_id in found:
            if note_id not in group:
                group[note_id] = mw.col.get_note(note_id)
    return group


def _own_notetype(note):
    """The note's own note type when it is one of ours, else the configured default.

    M on a German card must sweep GERMAN cards (user's rule, 2026-08-07: the known toggle
    works for German exactly as for Japanese) — the counter's `notetypes` list is the
    registry of "ours", and the singular `notetype` stays the fallback, the same pattern
    nav's L uses for the alternates view."""
    counter = conf.section("counter")
    try:
        name = note.note_type()["name"]
    except Exception:  # noqa: BLE001
        return counter["notetype"]
    return name if name in (counter.get("notetypes") or [counter["notetype"]]) \
        else counter["notetype"]


def word_group(word, tags, *, exclude=(), notetype=None):
    """The notes of `word` that are still in the queue — the precise group, by the one
    definition this project has: `queries.alt_query`, the set `L` shows.

    i+1 only. A card that also has *other* unknown morphs does not become known because one
    of them did; it becomes i+1, and that is recalc's job, not a tag sweep's. MvJ sweeps
    those too, by substring, which is how a card with two unknowns ends up marked known
    with both of them still unknown."""
    from .. import queries

    notetype = notetype or conf.section("counter")["notetype"]
    study_field = conf.section("counter")["study_field"]
    query = f'{queries.alt_query(notetype, study_field, word)} tag:"{tags["ready"]}"'
    try:
        found = mw.col.find_notes(query)
    except Exception as exc:  # noqa: BLE001
        log(f"group of {word!r} FAILED {exc!r}")
        return {}
    skip = set(exclude)
    return {nid: mw.col.get_note(nid) for nid in found if nid not in skip}


def mark_known(browser):
    """Mark the selected cards' word known. The entry point for the key **and** for every
    button of MvJ's that used to sweep by substring (`mvj_retunes._precise_tag_as_known`),
    so one press and one click do the same thing to the collection."""
    try:
        card_ids = list(browser.selectedCards())
    except Exception:  # noqa: BLE001
        card_ids = []
    if not card_ids:
        tooltip("Tag as known: no card selected")
        return
    _mark_known(browser, card_ids, _study_field(), conf.am_tags())


def sweep_similar(card_id, exclude_note_ids=None, morph_value=None):
    """The precise replacement for MvJ's `auto_tag_similar_cards`, which 💣 calls after it
    finishes a card: take the rest of that word's queue out with it. Returns the count, as
    its caller expects.

    Same substring bug, same fix — and the same restraint about i+≥2 cards: MvJ tags those
    known as well when they merely *contain* the morph, which marks a card known while a
    second unknown morph is still sitting in it.

    **The card 💣 was pressed on is marked known here, and that is the part that matters.**
    AnkiMorphs decides what is known from one tag — `tag_known_manually`, the only one its
    known-set search reads (`ankimorphs_db.py`) — and 💣 hands that card over in
    `exclude_note_ids` after taking its i+1 away, so without this nothing asserts the word
    at all: the next recalc finds the morph still unknown and puts the whole group back in
    the queue. The group's `i+0` is the *output* tag, cosmetic — it empties the queue now
    instead of at the next recalc, which then re-derives it anyway."""
    from .. import rules

    try:
        study_field = _study_field()
        note = mw.col.get_card(card_id).note()
        if morph_value is None:
            morph_value = note[study_field] if study_field in note else ""
        word = rules.norm_morph(morph_value)
        if not word:
            return 0
        tags = conf.am_tags()
        learn_tag = _learn_tag()
        chosen = []
        for note_id in exclude_note_ids or ():
            note = mw.col.get_note(note_id)
            _leave_queue(note, tags, learn_tag)
            if not note.has_tag(tags["known_manually"]):
                note.add_tag(tags["known_manually"])
                chosen.append(note)
        if chosen:
            mw.col.update_notes(chosen)
            log(f"sweep similar: {word} — {len(chosen)} card(s) marked known by hand")
        group = word_group(word, tags, exclude=exclude_note_ids or (),
                           notetype=_own_notetype(note))
        for note in group.values():
            _leave_queue(note, tags, learn_tag)
            if not note.has_tag(tags["known_auto"]):
                note.add_tag(tags["known_auto"])
        if group:
            mw.col.update_notes(list(group.values()))
        log(f"sweep similar: {word} — {len(group)} note(s)")
        return len(group)
    except Exception as exc:  # noqa: BLE001 — 💣 must not die on its tail step
        log(f"sweep similar FAILED {exc!r}")
        return 0


def _mark_known(browser, card_ids, study_field, tags):
    """Mark this word known — its own cards, and no others.

    **This used to be MvJ's `tag_as_known_in_editor`, and it swept by substring.** It
    collects the group with `am-study-morphs:*<word>*`, so marking 数 known also takes 数字,
    数学, 数える and every other morph containing it. Measured on the live collection, over
    i+1 cards only, its query against the precise one: 数 → 359 swept, 216 really that word,
    **143 wrong**; 生 → 879 against 56; 力 → 322 against 0. The user hit exactly the 143 and
    they had to be put back by hand.

    The precise shape already existed here — `queries.alt_query`, the set `L` shows, whose
    docstring carries the measurement that produced it (for 撮る: bare 204, union 205,
    substring 252). Marking and undoing are now the same set in two directions.

    Two writes, and only the first one *means* anything. AnkiMorphs derives "known" from
    the known-manually tag, so tagging the selected notes is what makes the word known;
    re-tagging the rest of the group is bookkeeping, so the queue updates now rather than at
    the next recalc. It is also self-correcting: recalc rebuilds these tags from the morphs
    themselves, and a card with one unknown morph and no ready tag gets the ready tag back
    (`tags_and_queue_utils`, `unknowns == 1`) — which is what makes an optimistic sweep safe
    and what would have healed MvJ's damage at the next recalc, had it been noticed."""
    from .. import rules

    known_m, known_a = tags["known_manually"], tags["known_auto"]
    learn_tag = _learn_tag()
    # Read before anything is written: these are the rows as the user still sees them,
    # and where the press should leave the cursor. See `_land_near` below.
    anchor = b.capture_anchor(browser)

    chosen = {}
    words = []
    word_notetype = {}
    for card_id in card_ids:
        note = mw.col.get_card(card_id).note()
        chosen[note.id] = note
        word = rules.norm_morph(note[study_field]) if study_field in note else ""
        if word and word not in words:
            words.append(word)
            word_notetype[word] = _own_notetype(note)
    if not chosen:
        tooltip("Tag as known: no card selected")
        return

    group = {}
    for word in words:
        for note_id, note in word_group(word, tags, exclude=chosen,
                                        notetype=word_notetype.get(word)).items():
            group.setdefault(note_id, note)

    for note in chosen.values():
        _leave_queue(note, tags, learn_tag)
        if not note.has_tag(known_m):
            note.add_tag(known_m)
    for note in group.values():
        _leave_queue(note, tags, learn_tag)
        if not note.has_tag(known_a):
            note.add_tag(known_a)

    mw.col.update_notes(list(chosen.values()) + list(group.values()))
    _land_near(browser, anchor)
    from .. import rules as _rules
    # 、 belongs to Japanese; German words in the tooltip read as a list, not a 熟語.
    sep = "、" if words and _rules.lang_of(words[0]) == "ja" else ", "
    named = sep.join(words) if words else "these cards"
    total = len(chosen) + len(group)
    # One number, not "1 + 271". The split is bookkeeping — which notes were selected and
    # which were swept with them — and the reader is being told how much of the queue just
    # went; that is the sum, and it was being left for them to do in their head.
    log(f"mark known: {named} — {total} note(s) ({len(chosen)} selected, {len(group)} swept)")
    tooltip(f"Known: {named} · {total} notes")


def _tag_as_known(browser):
    """The known toggle. Marked known → undo it, for the whole word; otherwise mark it
    known, for the whole word and nothing else (`_mark_known`, `_known_group`).

    **Both halves are this add-on's now.** The marking used to be MvJ's, and it swept the
    group by substring — 数 taking 数字, 数学, 数える with it, measured at 143 wrongly marked
    notes in one press. The two halves have to agree on what "this word's cards" means, or
    an undo cannot reverse a marking; they are the same query now, in two directions.

    Never fail silently — a dead key with no feedback is impossible to tell from a key that
    never fired."""
    study_field = _study_field()
    try:
        card_ids = list(browser.selectedCards())
        if not card_ids:
            tooltip("Tag as known: no card selected")
            return
        tags = conf.am_tags()
        known_m, known_a = tags["known_manually"], tags["known_auto"]

        first = mw.col.get_card(card_ids[0]).note()
        first_study = first[study_field].strip() if study_field in first else ""
        should_undo = first.has_tag(known_m) or (first.has_tag(known_a) and bool(first_study))
        if should_undo:
            anchor = b.capture_anchor(browser)
            to_restore = _known_group(card_ids, study_field, known_m, known_a)
            if not to_restore:
                # **Say so.** This used to report the count it had — including zero — as if
                # it were an undo, so a miss read exactly like a success.
                tooltip("Nothing to undo: no card of this word is marked known")
                log("undo known: nothing matched")
                return
            restored = 0
            for note in to_restore.values():
                if note.has_tag(known_m):
                    note.remove_tag(known_m)
                if note.has_tag(known_a):
                    note.remove_tag(known_a)
                study = note[study_field].strip() if study_field in note else ""
                if study:
                    sentence = study.split("|")[0].replace("Sentence:", "")
                    count = len([m for m in sentence.split(",") if m.strip()])
                    restore = tags["ready"] if count == 1 else tags["not_ready"]
                    if not note.has_tag(restore):
                        note.add_tag(restore)
                        restored += 1
            relearn = _restore_learn_tags(list(to_restore.values()), study_field,
                                          tags["ready"], _learn_tag())
            mw.col.update_notes(list(to_restore.values()))
            _land_near(browser, anchor)
            log(f"undo known: {len(to_restore)} note(s), {restored} put back in the queue, "
                f"{relearn} re-tagged my-learn")
            tooltip(f"Undid known on {len(to_restore)} note(s)")
            return
        _mark_known(browser, card_ids, study_field, tags)
    except Exception as exc:  # noqa: BLE001
        log(f"i: FAILED {exc!r}")
        tooltip(f"Tag toggle failed: {exc.__class__.__name__}: {exc}")


def _flag_and_translate(browser):
    """n: mark the card for later — red flag *and* MvJ's 🎰 Add Translation, one press.

    The flag half is Anki's own Cmd+1 action, toggle-off included. The translation only
    rides along when the press is *setting* the flag: un-flagging is how a mispress is
    taken back, and answering that with a paid AI call that overwrites the Notes field
    would make the undo cost more than the mistake. MvJ runs asynchronously off the
    current selection, so it starts from a timer — Anki's flag write is a CollectionOp of
    its own, and letting it be the thing in flight when MvJ reads the browser keeps the
    two out of each other's way."""
    card = getattr(browser, "card", None)
    if card is None:
        tooltip("Flag + translate: no card selected")
        return
    setting = card.user_flag() != 1
    browser.set_flag_of_selected_cards(1)
    if not setting:
        log("n: un-flagged, no translation")
        return

    def translate():
        try:
            addons.mvj("actions").smart_add_translation(browser)
        except Exception as exc:  # noqa: BLE001 — the flag landed; say why the rest didn't
            log(f"n: translation FAILED {exc!r}")
            tooltip(f"Flagged, but translation failed: {exc.__class__.__name__}: {exc}")

    QTimer.singleShot(0, translate)


def _trim_sentence_audio(browser):
    """u: open the Audio Trimmer on this card's Sentence Audio.

    Straight at that one field, not through the trimmer's own browser entry point: a MvJ
    note carries Word Audio and Definition Audio too, and that path answers three audio
    files with a picker. `_trim_audio_for_field` is the trimmer's own by-field opener — it
    saves the editor first and reloads the note afterwards, which is the part worth
    reusing rather than reimplementing."""
    editor = getattr(browser, "editor", None)
    note = getattr(editor, "note", None) if editor is not None else None
    if note is None:
        tooltip("Trim: select a single card first")
        return
    field = conf.mvj_field("sentence_audio", "Sentence Audio")
    names = list(note.keys())
    if field not in names:
        tooltip(f"Trim: this note has no '{field}' field")
        return
    try:
        addons.trimmer()._trim_audio_for_field(editor, names.index(field))
    except Exception as exc:  # noqa: BLE001
        log(f"u: trim FAILED {exc!r}")
        tooltip(f"Trim failed: {exc.__class__.__name__}: {exc}")


def _ask_question(browser):
    """y: MvJ's 🗣️ Ask Question on the selected card.

    One note only, which is MvJ's own rule rather than ours: it disables the menu entry
    for multi-selections, but `smart_add_explanation` reached any other way falls through
    to its bulk path. A key is exactly such a route, so the gate is repeated here."""
    try:
        notes = list(browser.selectedNotes())
    except Exception:  # noqa: BLE001
        notes = []
    if not notes:
        tooltip("Ask Question: no card selected")
        return
    if len(notes) > 1:
        tooltip(f"Ask Question: one card at a time ({len(notes)} selected)")
        return
    try:
        addons.mvj("actions").smart_add_explanation(browser)
    except Exception as exc:  # noqa: BLE001
        log(f"y: ask question FAILED {exc!r}")
        tooltip(f"Ask Question failed: {exc.__class__.__name__}: {exc}")


def _handlers(browser):
    """Action -> handler, shared by both delivery mechanisms."""
    return {
        "row_down": _stepper(browser, browser.table.to_next_row),
        "row_up": _stepper(browser, browser.table.to_previous_row),
        "known": lambda: _tag_as_known(browser),
        "flag": lambda: _flag_and_translate(browser),
        "trim": lambda: _trim_sentence_audio(browser),
        "ask": lambda: _ask_question(browser),
    }


def _logged(action, hotkey, source, handler):
    def run():
        log(f"{action} ({hotkey}) via {source}")
        handler()

    return run


class _VimKeys(QObject):
    """Deliver the keys via an event filter — see the module docstring.

    Covers the sidebar (a QShortcut bound to the card list cannot reach it) and backs up
    the QShortcut on the list itself. A filter rather than a second QShortcut: during an
    inline rename the keys go to the tree's own editor widget, which this filter never
    sees, so renaming a deck or tag still types normally."""

    def __init__(self, browser, source, hotkeys):
        super().__init__(browser)
        self._browser = browser
        handlers = _handlers(browser)
        self._handlers = {}
        for action, hotkey in hotkeys.items():
            key = _bare_key(hotkey)
            if key is not None and action in handlers:
                self._handlers[key] = _logged(action, hotkey, source, handlers[action])

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                handler = self._handlers.get(event.key())
                if handler:
                    # Reaching a filter at all means focus was not on the card list. These
                    # keys *are* card-list navigation, so they take the list with them:
                    # focus moves to the already-selected row, the press acts where it
                    # looks like it should, and the next one is native.
                    focus_card_list(self._browser)
                    handler()
                    return True
        return super().eventFilter(obj, event)


def _bind_keys(browser):
    """Bind whatever the config says, every time a browser opens.

    Re-read per browser rather than once at startup, so a key changed in MvJ's Hotkeys tab
    (or gigaku's config) takes effect on the next browser window instead of the next Anki
    launch."""
    hotkeys = _hotkeys()
    handlers = _handlers(browser)
    log(f"keys: {hotkeys}")
    # Bound to the table view, so a key typed in the search bar or an editor field never
    # reaches us.
    mine = []
    for action, hotkey in hotkeys.items():
        shortcut = QShortcut(QKeySequence(hotkey), browser.form.tableView)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(_logged(action, hotkey, "shortcut", handlers[action]))
        mine.append(shortcut)

    # Parented to the browser, so Qt keeps them alive for the window's lifetime.
    browser.sidebar.installEventFilter(_VimKeys(browser, "sidebar filter", hotkeys))
    browser.form.tableView.installEventFilter(_VimKeys(browser, "table filter", hotkeys))
    # Last resort, and the one that makes the keys reliable: see _VimKeys.
    browser.installEventFilter(_VimKeys(browser, "window filter", hotkeys))
    # This must never be able to stop the browser from opening.
    try:
        _disable_rivals(browser, mine, hotkeys)
        _silence_unkillable_nav(browser)
        _log_key_claims(browser)
    except Exception as exc:  # noqa: BLE001
        log(f"rival scan FAILED {exc!r}")


def _disable_rivals(browser, mine, hotkeys):
    """Silence anything else bound to the keys this feature is *actually* on.

    Read off the resolved hotkeys rather than a hardcoded list, which is what keeps it
    honest now that the keys are configurable: it silences a rival on a key we really took,
    and never on a letter we merely used to take. MvJ binds its own Tag as Known and its
    e/i row movement, and one key must mean one thing — but a key this feature no longer
    holds is none of its business.

    nav's own hotkeys are deliberately not swept for: a config that put two gigaku actions
    on one key would otherwise have one feature silently killing another's shortcut.

    **QActions are swept too, and that half is what makes the Hotkeys tab usable.** MvJ
    binds its Tag as Known as a menu QAction rather than a QShortcut ("handled in browser
    menu via QAction to avoid conflicts", its own comment) — so setting that row to a key
    this feature also holds gives Qt two claims on it and it fires neither. The action
    keeps its menu entry and loses only its key, which is the right half to lose: the entry
    still works by clicking, and the key does the toggle rather than the one-way version."""
    taken = {QKeySequence(hotkey).toString() for hotkey in hotkeys.values()}
    ours = {id(shortcut) for shortcut in mine}
    for shortcut in browser.findChildren(QShortcut):
        key = shortcut.key().toString()
        if key not in taken or id(shortcut) in ours:
            continue
        # QShortcut is a QObject, not a QWidget: parent(), never parentWidget().
        parent = shortcut.parent()
        name = parent.objectName() if parent is not None else None
        log(f"disabling rival {key}: parent={type(parent).__name__} name={name!r}")
        shortcut.setEnabled(False)
    for action in browser.findChildren(QAction):
        key = action.shortcut().toString()
        if not key or key not in taken:
            continue
        # A menu entry keeps its entry and loses only the key. This is the half that lets a
        # key be *ours*: MvJ binds its Tag as Known as an action, and an action and a
        # shortcut on one key are two claims, which Qt answers by firing neither.
        log(f"clearing rival {key} off action {action.text()!r}")
        action.setShortcut(QKeySequence())


def _silence_unkillable_nav(browser):
    """Disable MvJ's own row movement when its key is claimed by something else.

    **They used to be unreachable from anywhere**, which is what this was written for, and
    `features/hotkey_ui.py` has since fixed that at the source: MvJ's loader keeps them now
    and its dialog shows them, so a collision is the user's to resolve. This stays as the
    guard for the moment before they do — two live claims fire nothing, silently, and that
    failure should not be how they find out. Historically: MvJ loads its hotkeys as
    `HotkeyConfig(**{k: v for k, v in saved.items() if k in allowed_hotkeys})`, and
    `browser_navigate_up`, `browser_navigate_down` and `browser_focus_sentence` are not in
    `allowed_hotkeys` — so whatever its config file holds is dropped on load and the
    dataclass defaults win: `i`, `e`, `n`. They appear in no dialog, and editing the config
    does nothing, which is why clearing `browser_navigate_up` by hand changed nothing.

    That is what "i does nothing" was. The user put View Context on `i` in MvJ's own Hotkeys
    tab; MvJ bound the action there **and** kept its unkillable navigate-up on the same key,
    and Qt answers two live claims by firing neither. Read out of the live browser by
    `_log_key_claims` after four wrong guesses — no configuration file could have shown it.

    A nav shortcut is silenced only where something else claims its key: a menu entry is
    discoverable and switchable, this is neither. All three are replaced here anyway."""
    try:
        defaults = addons.mvj("config").HotkeyConfig()
    except Exception as exc:  # noqa: BLE001
        log(f"nav rivals: MvJ's defaults unreadable ({exc!r})")
        return
    nav = {QKeySequence(getattr(defaults, name, "") or "").toString()
           for name in ("browser_navigate_up", "browser_navigate_down",
                        "browser_focus_sentence")}
    nav.discard("")
    if not nav:
        return
    # Both scans walk the whole widget tree, so the cheap one goes first and the second
    # never runs unless a menu entry actually claims one of these keys.
    claimed = {action.shortcut().toString() for action in browser.findChildren(QAction)
               if action.shortcut().toString()} & nav
    if not claimed:
        return
    for shortcut in browser.findChildren(QShortcut):
        key = shortcut.key().toString()
        if key in claimed and shortcut.isEnabled():
            log(f"nav rivals: disabling MvJ's unkillable {key!r} — a menu action claims it")
            shortcut.setEnabled(False)


def _log_key_claims(browser):
    """Log every key the browser has a claim on, and flag the ones claimed twice.

    Qt answers two claims on one key by firing **neither**, silently. That single fact was
    every hotkey failure of one long evening — this add-on's key against MvJ's, and finally
    MvJ's View Context against MvJ's *own* undisplayed `browser_navigate_up`, both on `i`,
    which no amount of reading either add-on's settings would show: one of the two rows is
    not in its dialog. Reading the live Qt state is the only thing that answers "what is
    actually bound", so it is written down rather than deduced. Debug-gated, so it costs a
    config read when it is off."""
    if not conf.debug():
        return
    claims = {}
    try:
        for shortcut in browser.findChildren(QShortcut):
            key = shortcut.key().toString()
            if key:
                state = "on" if shortcut.isEnabled() else "OFF"
                claims.setdefault(key, []).append(
                    f"QShortcut<{type(shortcut.parent()).__name__}> {state}")
        for action in browser.findChildren(QAction):
            key = action.shortcut().toString()
            if key:
                # A *disabled* action is the quiet one: its key does nothing at all, with no
                # tooltip and no error — MvJ disables its View Context whenever the
                # selection is not exactly one card.
                state = "on" if action.isEnabled() else "OFF (disabled action)"
                claims.setdefault(key, []).append(f"QAction<{action.text()}> {state}")
    except Exception as exc:  # noqa: BLE001
        log(f"key claims: scan FAILED {exc!r}")
        return
    for key, holders in sorted(claims.items()):
        live = [h for h in holders if h.endswith("on")]
        note = ("" if len(live) == 1 else
                "  <-- NOTHING WILL FIRE" if len(live) != 1 else "")
        log(f"key claims: {key!r}: {holders}{note}")


def _then_focus_card_list(method):
    def wrapped(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        focus_card_list(self)
        return result

    return wrapped


def install():
    gui_hooks.browser_will_show.append(_bind_keys)
    # Every arrival at a fresh search ends focused on the card list. Guarded patches —
    # the old add-on wrapped these unconditionally at import, its one unguarded patch.
    patch_once(Browser, "setupSearch", _then_focus_card_list, "_gigaku_focus_setup")
    patch_once(Browser, "reopen", _then_focus_card_list, "_gigaku_focus_reopen")
