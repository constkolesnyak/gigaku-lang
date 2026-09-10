"""MvJ's 🔗 Merge Cards keeps the merged cards' am-study-morphs
(was study_morph_counter's merge retune).

MvJ merges Sentence + Sentence Audio into one card and deletes the rest, leaving
am-study-morphs untouched — so the word a deleted card was teaching disappears from i+1
and my-learn until the next recalc. Worse, the receiver is chosen by scheduling, which in
an episode-context view is normally the *neighbouring* sentence, not the i+1 card being
improved. The union written here is what AnkiMorphs itself will compute from the merged
sentence at the next recalc, so nothing is invented — it is anticipated.
"""
from aqt import gui_hooks, mw

from ..core import addons, conf
from ..core.log import log
from ..core.patching import patch_once


def _cfg():
    counter = conf.section("counter")
    tags = conf.am_tags()
    return {
        "study_field": counter["study_field"],
        "learn_tag": counter["learn_tag"],
        "ready_tag": tags["ready"],
        "not_ready_tag": tags["not_ready"],
        "known_auto_tag": tags["known_auto"],
        "known_manual_tag": tags["known_manually"],
    }


def _morph_list(value):
    return [morph.strip() for morph in (value or "").split(",") if morph.strip()]


def _carry_study_morphs(browser):
    """Give MvJ's merge receiver every merged card's am-study-morphs, before the merge.

    The status tags follow from the union, because the field alone doesn't put the card
    back in the study queue: one sentence morph is i+1, two or more is i+≥2, and either
    way it is no longer i+0. A card the user declared known by hand is left alone entirely
    — that tag is an *input* to AnkiMorphs, not a verdict of its own. ``my-learn`` moves
    across too when the merged card is still a single word, so improving the card you
    study doesn't drop the word out of the main view until the next recalc."""
    cfg = _cfg()
    cids = list(browser.selectedCards())
    if len(cids) < 2:
        return  # MvJ refuses the merge too; don't write anything it won't act on
    study = cfg["study_field"]
    receiver_nid = mw.col.get_card(addons.mvj("services.merge").select_receiver_card(cids)).nid

    notes, seen = [], set()
    for cid in sorted(cids):  # card-id order — the order MvJ merges the sentences in
        note = mw.col.get_card(cid).note()
        if note.id not in seen:
            seen.add(note.id)
            notes.append(note)
    receiver = next((note for note in notes if note.id == receiver_nid), None)
    if receiver is None or study not in receiver:
        return

    parse = addons.mvj("utils.morphs").parse_study_morphs_field
    sentence_morphs = []
    def_morphs = []
    prefixed = False
    for note in notes:
        if study not in note:
            continue
        raw = note[study]
        prefixed = prefixed or "Sentence:" in raw or "Definition:" in raw
        from_sentence, from_def = parse(raw)
        for morph in _morph_list(from_sentence):
            if morph not in sentence_morphs:
                sentence_morphs.append(morph)
        for morph in _morph_list(from_def):
            if morph not in def_morphs:
                def_morphs.append(morph)
    had_learn = (any(note.has_tag(cfg["learn_tag"]) for note in notes)
                 if cfg["learn_tag"] else False)

    # Keep the shape the cards already used: MvJ's prefixes only appear once a definition
    # has contributed unknowns, and adding them to a plain AnkiMorphs value would change
    # what a whole-field search (L's, and MvJ's own) matches.
    if prefixed or def_morphs:
        value = addons.mvj("utils.morphs").format_study_morphs_field(
            ", ".join(sentence_morphs), ", ".join(def_morphs))
    else:
        value = ", ".join(sentence_morphs)

    changed = False
    if receiver[study].strip() != value.strip():
        log(f"merge: {receiver[study]!r} + donors -> {value!r}")
        receiver[study] = value
        changed = True

    if sentence_morphs and not receiver.has_tag(cfg["known_manual_tag"]):
        single = len(sentence_morphs) == 1
        want = cfg["ready_tag"] if single else cfg["not_ready_tag"]
        for tag in (cfg["not_ready_tag"] if single else cfg["ready_tag"], cfg["known_auto_tag"]):
            if tag and receiver.has_tag(tag):
                receiver.remove_tag(tag)
                changed = True
        if want and not receiver.has_tag(want):
            receiver.add_tag(want)
            changed = True
        if had_learn and single and not receiver.has_tag(cfg["learn_tag"]):
            receiver.add_tag(cfg["learn_tag"])
            changed = True

    if changed:
        mw.col.update_note(receiver)


def _patch_merge(_browser=None):
    """Wrap MvJ's ``actions.merge_cards``, self-guarded and never fatal.

    Patched as a module attribute: MvJ wires the merge as
    ``lambda: actions.merge_cards(browser)`` from both its browser menu and its context
    menu, and both resolve the name at call time, so one swap catches both."""
    try:
        actions = addons.mvj("actions")

        def wrap(original):
            def merge_cards(browser):
                try:
                    _carry_study_morphs(browser)
                except Exception as exc:  # noqa: BLE001 — never block the merge itself
                    log(f"merge: carrying am-study-morphs failed: {exc!r}")
                original(browser)

            return merge_cards

        if patch_once(actions, "merge_cards", wrap, "_gigaku_merge_keeps_morphs",
                      legacy_flags=("_smc_merge_keeps_morphs",)):
            log("merge: retuned to keep am-study-morphs")
    except Exception as exc:  # noqa: BLE001
        log(f"merge: retune FAILED {exc!r}")


def install():
    # On browser open rather than at import: the patch needs MvJ loaded, and it has to be
    # in place however many browsers have opened.
    gui_hooks.browser_menus_did_init.append(_patch_merge)
