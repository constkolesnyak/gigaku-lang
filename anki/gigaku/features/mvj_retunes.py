"""Four retunes of MvJ itself (was browser_jk_nav's MvJ half): the mass-auto-tag prompt
never appears, H plays only the Sentence Audio field, O ('View Context 🎬') becomes a
toggle that restores search, mode, selection and **flag values** on the way back — and
O's own re-sort of the episode is removed at the source, so the episode order arrives with
the search that builds the view (`browser_view` + `conf.view_sort`) instead of as a second
sort and a second search 100ms behind it.

All patched in place, guarded by sentinels (the old add-on's included, so a mixed state
during migration can never double-patch); MvJ isn't in this repo, so a source edit would
be wiped by an add-on update — these survive it.
"""
import time

from aqt import gui_hooks, mw
from aqt.browser import Browser

from ..core import addons, conf
from ..core import browser as b
from ..core.log import log
from ..core.patching import patch_once


def _silence_mass_tag_confirm():
    """Skip MvJ's 'Auto-tag would affect N cards' prompt.

    Both presses that reach it (i, and All In One 💣) are deliberate and the sweep is what
    they are *for* — a word with 400 alternatives is exactly the case worth
    de-duplicating — so the prompt is friction on the common path, not a safety net.
    Applied on browser open rather than lazily from the i handler: it is a module
    attribute, so the first press used to silence it for every later caller anyway, which
    made the bomb's prompt a coin flip on whether i had been pressed yet that session."""
    try:
        media = addons.mvj("services.media")
        if patch_once(media, "_confirm_mass_auto_tag", lambda original: (lambda *a, **k: True),
                      "_gigaku_confirm_silenced", legacy_flags=("_jk_confirm_silenced",)):
            log("silenced _confirm_mass_auto_tag")
    except Exception as exc:  # noqa: BLE001
        log(f"silence confirm FAILED {exc!r}")


def _restrict_play_audio_to_sentence():
    """MvJ's browser hotkey H ('▶️ Play Audio') plays *every* field's audio; make it play
    only the Sentence Audio field, via MvJ's own sentence-only helper (the exact code
    behind its X hotkey). Patched on the class once, so it survives across windows."""
    try:
        cls = addons.mvj("services.media").MediaService
        actions = addons.mvj("actions")

        def wrap(_original):
            def play_audio_in_browser(self, browser):
                actions.play_sentence_audio_browser(browser)

            return play_audio_in_browser

        if patch_once(cls, "play_audio_in_browser", wrap,
                      "_gigaku_sentence_only", legacy_flags=("_jk_sentence_only",)):
            log("restricted H (Play Audio) to Sentence Audio only")
    except Exception as exc:  # noqa: BLE001
        log(f"restrict play-audio FAILED {exc!r}")


# ── reversible O ─────────────────────────────────────────────────────────────


def _green_flagged():
    """Cards currently carrying flag 3 (green) — MvJ's "you came from here" marker."""
    try:
        return mw.col.db.list("SELECT id FROM cards WHERE (flags & 7) = 3")
    except Exception:  # noqa: BLE001
        return []


def _flags_of(card_ids):
    """card id -> its user flag (0-7).

    A card's flag is a single *value*, not a set of colours, so MvJ's green marker doesn't
    sit alongside a red flag — it replaces it. Recording the value is therefore the only
    way to give it back."""
    ids = list(dict.fromkeys(cid for cid in card_ids if cid is not None))
    if not ids:
        return {}
    try:
        placeholders = ",".join("?" * len(ids))
        rows = mw.col.db.all(
            f"SELECT id, (flags & 7) FROM cards WHERE id IN ({placeholders})", *ids)
        return {cid: flag for cid, flag in rows}
    except Exception as exc:  # noqa: BLE001
        log(f"read flags FAILED {exc!r}")
        return {}


def _capture_view(browser):
    """Everything MvJ's View Context changes, read back before it runs."""
    try:
        cids = list(browser.selectedCards())
    except Exception:  # noqa: BLE001
        cids = []
    state = getattr(browser.table, "_state", None)
    return {
        "search": getattr(browser, "_lastSearchTxt", None) or b.searchbar_text(browser),
        # The **current** row and its neighbours, not just the first selected card — the
        # same anchor L's way out takes, for the same reasons: the way back is a *return*
        # (so it is the focused row that has to come back, not the head of a multi-row
        # selection), the cards are re-found by id in whatever the restored view holds (so
        # it survives O's switch to Cards mode), and the card itself is routinely gone by
        # then — the round trip usually ends with its word marked known.
        "anchor": b.capture_anchor(browser),
        "notes_mode": b.notes_mode(browser),
        "sort_column": getattr(state, "sort_column", None),
        "sort_backwards": getattr(state, "sort_backwards", None),
        # The flag *values* of every card O can repaint: whatever is green now (MvJ clears
        # green collection-wide) and the selection it is about to mark green. Recording
        # only "which cards were green" is what lost a red flag — see _restore_flags.
        "flags": _flags_of(_green_flagged() + cids),
    }


def _restore_flags(saved):
    """Give every card back the flag it had before O moved the green marker.

    This used to restore "which cards were green" and clear the rest — which set the card
    you jumped from to **no flag**, silently deleting the red flag that `n` had put on it.
    Two presses of O and the mark was gone. So the values are recorded and put back as
    values. Green that turned up on a card nothing was recorded for is still cleared: it
    can only be MvJ's own marker, and the jump is what is being undone."""
    targets = dict(saved)
    for card_id in _green_flagged():
        targets.setdefault(card_id, 0)
    if not targets:
        return
    current = _flags_of(targets)
    by_flag = {}
    for card_id, flag in targets.items():
        if current.get(card_id, flag) != flag:
            by_flag.setdefault(flag, []).append(card_id)
    try:
        for flag, card_ids in by_flag.items():
            log(f"restoring flag {flag} on {len(card_ids)} card(s)")
            mw.col.set_user_flag_for_cards(flag, card_ids)
    except Exception as exc:  # noqa: BLE001
        log(f"restore flag FAILED {exc!r}")


def _restore_view(browser, saved):
    """Put the browser back where the first O left from — in one search, no waiting.

    Every step here is synchronous, which is why nothing is scheduled: `_switch.setChecked`
    runs Anki's mode toggle (and its re-search) inline, and `search_for` runs the search
    inline. The landing is asked for *before* the search and consumed by it the moment it
    finishes (`b.land_after`), instead of being fired at a card 100ms later and then
    quietly overwritten by Anki's own selection restore — which is what made the way back
    return the focus only sometimes.

    The sort is put back only if it moved: the episode context carries its order on the
    search and never writes the state, so coming back from a normal round trip there is
    nothing to restore — this covers the case where the user clicked a header *inside* the
    context view, which does write it."""
    _restore_flags(saved["flags"])

    if saved["notes_mode"] != b.notes_mode(browser):
        try:
            browser._switch.setChecked(saved["notes_mode"])
        except Exception as exc:  # noqa: BLE001
            log(f"restore mode FAILED {exc!r}")

    if saved["sort_column"]:
        b.remember_order(browser, saved["sort_column"],
                         descending=bool(saved["sort_backwards"]))

    anchor = list(saved.get("anchor") or ())
    b.land_after(browser, b.select_anchor(anchor) if anchor else b.keep_selection)
    try:
        browser.search_for(saved["search"])
    except Exception as exc:  # noqa: BLE001
        log(f"restore search FAILED {exc!r}")
        b.take_landing(browser)


def leave_context(browser):
    """Undo what entering the episode context did, for a way out that is not O.

    **`L` is such a way out**, and it is the common one: the study loop is L → O → edit →
    L, so the context view is routinely left by the key that goes home rather than by a
    second O. Nothing undid the visit on that path — the green flag MvJ paints on the card
    you jumped from stayed lit, and the remembered return point stayed on the browser,
    where the next O would find it stale.

    Only the flags and the remembered point: where to land is the caller's business, and
    `nav` already knows it (it has its own anchor for the way out)."""
    saved = getattr(browser, "_gigaku_ctx_state", None)
    browser._gigaku_ctx_state = None
    browser._gigaku_ctx_no_sort_until = 0
    if saved is None:
        # No return point of ours, but the green flag is MvJ's and is lit either way.
        _restore_flags({})
        return
    log("O: context view left by another key — flags put back, return point dropped")
    _restore_flags(saved["flags"])


def _in_context_view(browser):
    from .. import queries

    search = (getattr(browser, "_lastSearchTxt", None) or b.searchbar_text(browser))
    return queries.is_context_query(conf.mvj_field("sentence_audio", "Sentence Audio"), search)


def _fallback_view(browser):
    """Where O returns to when it has no return point of its own — the main working
    search. A context view can outlive the press that opened it (a restart, or L walking
    out of it and back in), and "nothing remembered" must not mean "stuck". Cards/Notes
    mode is left as it is, since there is nothing to restore it to."""
    body = conf.mvj_browser_query()
    if not body:
        return None
    # Scoped to the selected card's note type — a German episode must fall back into the
    # German queue, not a mixed one (the same clause home_query exists for).
    from .. import queries
    counter = conf.section("counter")
    notetype = counter["notetype"]
    try:
        cids = browser.table.get_selected_card_ids()
        if cids:
            name = browser.col.get_card(cids[0]).note().note_type()["name"]
            if name in (counter.get("notetypes") or [notetype]):
                notetype = name
    except Exception:  # noqa: BLE001
        pass
    query = queries.home_query(body, conf.am_tags()["ready"], notetype)
    return {"search": query, "anchor": [], "notes_mode": b.notes_mode(browser),
            "sort_column": None, "sort_backwards": None, "flags": {}}


def _suppress_context_sort():
    """Remove O's episode re-sort — the order it wanted now arrives with the search.

    MvJ's `view_context` schedules `show_and_sort_by_column(browser, "noteCrt",
    descending=False)` on a 0–100ms QTimer, and that helper shows the column, writes the
    sort state and calls `browser.search()` again. **The order it asks for is the right
    one** — an episode reads in the order it was watched — so it is not overridden but
    moved: `conf.view_sort("context")` names the same column and direction, and
    `browser_view` puts it on the search that builds the view. What is dropped is the
    second search, the extra Created column, and a sort landing 100ms after the rows the
    user is already reading.

    The helper has other callers (MvJ's cardDue sorts), so it is not blanket-disabled: the
    O wrapper *arms* a short-lived flag on the browser, and only the one armed call is
    swallowed — the select-and-scroll half of O's tail still runs, so the pressed card is
    still centered.
    """
    try:
        actions = addons.mvj("actions")

        def wrap(original):
            def show_and_sort_by_column(browser, *args, **kwargs):
                if time.monotonic() < getattr(browser, "_gigaku_ctx_no_sort_until", 0):
                    browser._gigaku_ctx_no_sort_until = 0
                    log("O: episode sort suppressed — the remembered order stands")
                    return
                original(browser, *args, **kwargs)

            return show_and_sort_by_column

        if patch_once(actions, "show_and_sort_by_column", wrap, "_gigaku_ctx_sort_off"):
            log("removed O's episode re-sort")
    except Exception as exc:  # noqa: BLE001
        log(f"suppress context sort FAILED {exc!r}")


def _make_view_context_reversible():
    """Make MvJ's O a toggle, the way L is.

    O is one-way: green flag, Cards mode, an episode-wide search sorted by creation date —
    with no way back but retyping the search. Second press now returns everything.
    **Which of the two a press means is read off the screen, not off remembered state**,
    for the reason the L toggle learned first: remembered state desyncs, and asking the
    search cannot — a press outside a context view always enters, overwriting leftovers.

    MvJ's own function bails out early — tooltip, no change — when the selection isn't
    exactly one card or the audio field can't be parsed. It sets the search bar
    synchronously when it does act, so an unchanged search bar afterwards means nothing
    happened and the remembered state is dropped; otherwise the next O would "restore" a
    jump never made."""
    try:
        actions = addons.mvj("actions")

        def wrap(original):
            def view_context(browser):
                if _in_context_view(browser):
                    saved = getattr(browser, "_gigaku_ctx_state", None) or _fallback_view(browser)
                    browser._gigaku_ctx_state = None
                    if saved is None:
                        log("O: context view, no return point, no MvJ search — stayed")
                        return
                    log("O: restoring pre-context view "
                        f"(anchor={list(saved.get('anchor') or ())[:1] or None})")
                    _restore_view(browser, saved)
                    return
                saved = _capture_view(browser)
                before = b.searchbar_text(browser)
                # Arm the sort suppressor for the 0–100ms timer O is about to schedule
                # (_suppress_context_sort); disarmed below if MvJ declines, and the
                # deadline keeps a missed disarm from ever swallowing a later, legitimate
                # sort from another MvJ action.
                browser._gigaku_ctx_no_sort_until = time.monotonic() + 2.0
                # Land on the card O was pressed on the moment the episode search finishes.
                # MvJ selects it too, 100ms later, from its own timer — the same card, so
                # its late shot is a no-op instead of the thing the eye waits for.
                if saved["anchor"]:
                    b.land_after(browser, b.select_anchor(saved["anchor"][:1]))
                original(browser)
                if b.searchbar_text(browser) == before:
                    browser._gigaku_ctx_no_sort_until = 0
                    b.take_landing(browser)
                    # MvJ moves the green flag *before* it parses the audio field, so a
                    # decline can still have moved it. Put it back, so "declined" really
                    # means nothing happened.
                    _restore_flags(saved["flags"])
                    log("O: MvJ declined, nothing remembered")
                    return
                browser._gigaku_ctx_state = saved
                log("O: context view entered, return point remembered "
                    f"(anchor={saved['anchor'][:1] or None}, "
                    f"+{max(0, len(saved['anchor']) - 1)} neighbours)")

            return view_context

        if patch_once(actions, "view_context", wrap,
                      "_gigaku_ctx_reversible", legacy_flags=("_jk_ctx_reversible",)):
            log("made O (View Context) reversible")
    except Exception as exc:  # noqa: BLE001
        log(f"reversible O FAILED {exc!r}")


def _browser_of(editor):
    """The Browser an editor belongs to, or None — MvJ's own way of asking."""
    try:
        window = editor.widget.window() if getattr(editor, "widget", None) else None
    except Exception:  # noqa: BLE001
        return None
    return window if isinstance(window, Browser) else None


def _precise_tag_as_known():
    """Every way into MvJ's "tag as known" marks the word, not a substring of it.

    Its own version collects the group with `am-study-morphs:*<word>*`, so marking 数 known
    takes 数字, 数学, 数える with it. Measured over i+1 cards: 数 → 359 swept against 216 that
    really are that word, 生 → 879 against 56, 力 → 322 against 0. The key stopped calling
    it, which fixed the key and nothing else — **three entry points still reached it**: the
    editor button, the editor's own shortcut and the browser menu's "✅ Tag as Known". They
    all funnel through this one method, so one patch covers all three, and a click and a
    press now do the same thing to the collection.

    Anything that is not a browser with a selection is left to the original, which refuses
    it politely ("Not in browser context") — that refusal is MvJ's to give."""
    try:
        cls = addons.mvj("services.media").MediaService

        def wrap(original):
            def tag_as_known_in_editor(self, editor):
                from . import keys

                browser = _browser_of(editor)
                if browser is None:
                    return original(self, editor)
                keys.mark_known(browser)

            return tag_as_known_in_editor

        if patch_once(cls, "tag_as_known_in_editor", wrap, "_gigaku_precise_known"):
            log("MvJ's tag-as-known now marks the word, not a substring of it")
    except Exception as exc:  # noqa: BLE001
        log(f"precise tag-as-known FAILED {exc!r}")


def _precise_auto_tag():
    """The second sweep, with the same substring bug: `auto_tag_similar_cards`, which
    **💣 All In One** calls after it finishes a card, to take the rest of that word's queue
    out with it. Same replacement, and the same restraint about cards that have a second
    unknown morph — those become i+1, not known, and that is recalc's job."""
    try:
        cls = addons.mvj("services.media").MediaService

        def wrap(_original):
            def auto_tag_similar_cards(self, card_id, exclude_note_ids=None,
                                       morph_value=None, parent_window=None):
                from . import keys

                return keys.sweep_similar(card_id, exclude_note_ids, morph_value)

            return auto_tag_similar_cards

        if patch_once(cls, "auto_tag_similar_cards", wrap, "_gigaku_precise_sweep"):
            log("MvJ's 💣 sweep now marks the word, not a substring of it")
    except Exception as exc:  # noqa: BLE001
        log(f"precise auto-tag FAILED {exc!r}")


def _apply(_browser=None):
    _silence_mass_tag_confirm()
    _precise_tag_as_known()
    _precise_auto_tag()
    _restrict_play_audio_to_sentence()
    _suppress_context_sort()
    _make_view_context_reversible()


def install():
    gui_hooks.browser_will_show.append(_apply)
