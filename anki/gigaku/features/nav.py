"""J (Japanese queue), K (German queue) and L (alternates) — study_morph_counter's browser half.

One key per language, and neither view ever shows the other's cards: my-learn and the
status tags are collection-wide, so every queue query is scoped to its note type
(user's rule, 2026-08-06 — "do not mix languages"). L works in both queues: the alternates
query takes its note type from the selected card, and the way back out recognises either
language's alternates view.

The study loop is main view → L → O → edit → back. Both ways down are *drilled-in views*
that L comes out of, and which of the two a press means is decided from **what is on
screen** (the running search), never from remembered state — remembered state kept
desyncing from the view, and each desync had its own failure mode (see
`browse_alternates_toggle`).

A press is now two lines: say where the selection should land, run the search. The order
and the columns are `browser_view`'s answer, given to that same search before it runs, and
the landing happens the moment it finishes — so a press costs one search and no waiting.
Nothing here sorts, re-sorts or re-checks any more.
"""
from aqt import dialogs, gui_hooks, mw
from aqt.qt import QKeySequence, QShortcut, Qt, QTimer

from .. import queries, rules
from ..core import browser as b
from ..core import conf
from ..core.log import log

_goto_shortcuts = []

def _cfg():
    section = conf.section("nav")
    counter = conf.section("counter")
    section.update(
        notetype=counter["notetype"],
        study_field=counter["study_field"],
        ready_tag=conf.am_tags()["ready"],
    )
    # These keys come from this add-on's own config and nowhere else. Reading MvJ's
    # Hotkeys rows for them was tried and taken back out: a filled row is what makes MvJ
    # bind *its* Go-to Browser Search and *its* Browse Alternates, so every row that named
    # a key this side already held put two claims on it, and Qt answers two claims by
    # firing neither.
    return section


def _order_first(browser, kind="queue"):
    """Put the remembered order in the sort state *before* the search runs.

    Not an optimisation — it is where the order has to be set. Anki resolves a search's
    order from the sort state at the top of `_search_inner`, before any add-on's hook, and
    the remembered column is one of Advanced Browser's (`_field_*`), which only Advanced
    Browser can turn into something the backend will sort by. Setting the state here means
    the one search this press runs is already in the right order; `browser_view` would
    otherwise notice the drift too late and re-run it."""
    key, descending = conf.view_sort(kind)
    b.remember_order(browser, key, descending=descending)


def _notetypes(cfg):
    """Every note type with a queue — counter's list is the registry of 'our' note types."""
    return tuple(conf.section("counter").get("notetypes") or [cfg["notetype"]])


def _home_query(cfg, notetype=None):
    # The body (MvJ's query, today `tag:my-learn`) is shared: my-learn means the same
    # thing in every language, and the note-type clause is what keeps the queues apart.
    return queries.home_query(conf.mvj_browser_query(), cfg["ready_tag"],
                              notetype or cfg["notetype"])


def _sentence_audio_field():
    return conf.mvj_field("sentence_audio", "Sentence Audio")


def goto_browser_search(notetype=None, query=None):
    """J/K from the main window (browser closed only): open the Browser on that queue —
    or on `query`, when the key is configured to open a search of its own."""
    if b.browser_open():
        return  # let the Browser keep j/k = row moves; don't reopen/steal focus
    query = query or _home_query(_cfg(), notetype)
    # **Opened on the queue, not opened and then sent there.** A Browser built with no
    # search runs its own default one first (`Browser.setupSearch`), so J paid for two
    # searches of a 460k-card collection to show one view — and the first was over
    # whatever the last search had been, in whatever order, which is the one search
    # `browser_view` cannot make cheap. Anki takes the query as an argument; passing it
    # means the browser has never shown anything else.
    browser = dialogs.open("Browser", mw, search=(query,))
    # The search already ran inside that call, so there is nothing to hand a landing to:
    # land now, on the view that is already up.
    b.select_top(browser)
    b.focus_card_list(browser)
    browser.activateWindow()
    browser.raise_()


def browse_alternates_toggle(browser):
    """L in the Browser: show only this word's cards; L again comes back.

    Which of the two a press means is decided from **what is on screen**. Remembered state
    kept desyncing from the view and each desync had its own failure: left behind after
    MvJ's `O` it made L restore a search the user had moved on from; dropped while still
    inside the alternates view it stranded the view with no way back, and the next press
    then recorded *the alternates query itself* as "previous", turning L into a permanent
    no-op. Reading the current search cannot desync: a drilled-in view is recognisable by
    the query that built it, so L in one always comes back out, and L anywhere else always
    opens one.

    **Both** ways down count as drilled in — the alternates view and MvJ's `O` episode
    context — because `O` is where the editing happens: while a context view counted as an
    ordinary view, L there *entered* alternates and pinned home to the episode search, and
    the main view became unreachable without retyping it.

    **Either way out ends in the remembered sort order, back on the card the round trip
    started from** — or, once that card has left the view (the usual ending: its word is
    now known), on its nearest surviving neighbour, downwards first. The way out is a
    *return*; the top row is what an arrival with nothing to return to gets."""
    cfg = _cfg()
    current = b.current_search_text(browser)
    # Which language's alternates view this is (if any) — the way back out must exist
    # from a German drill-down exactly as from a Japanese one.
    alt_notetype = next((nt for nt in _notetypes(cfg)
                         if queries.is_alt_query(nt, cfg["study_field"], current)), None)
    if alt_notetype or queries.is_context_query(_sentence_audio_field(), current):
        if queries.is_context_query(_sentence_audio_field(), current):
            # Leaving the episode context by this key rather than by a second O: the visit
            # still has to be undone — see `mvj_retunes.leave_context`.
            from .mvj_retunes import leave_context
            leave_context(browser)
        home = getattr(browser, "_gigaku_home", None)
        prev_search = home or _home_query(cfg, alt_notetype)
        # The anchor belongs to the remembered search: with no search to go back to there
        # is no view those cards were rows of, so it is not carried to the fallback's.
        anchor = list(getattr(browser, "_gigaku_home_cards", None) or ()) if home else []
        log(f"L: leaving -> {prev_search!r} "
            f"(remembered={home is not None}, anchor={anchor[:1] or None})")
        browser._gigaku_home = None
        browser._gigaku_home_cards = []
        _order_first(browser)
        b.land_after(browser, b.select_anchor(anchor) if anchor else b.select_top)
        browser.search_for(prev_search)
        return

    try:
        cids = browser.table.get_selected_card_ids()
    except Exception:  # noqa: BLE001
        cids = list(browser.selectedCards()) if hasattr(browser, "selectedCards") else []
    if not cids:
        return
    note = mw.col.get_card(cids[0]).note()
    study_field = cfg["study_field"]
    if study_field not in note:
        return
    word = rules.norm_morph(note[study_field])
    if not word:
        return
    # The alternates of a German card are German cards: the query's note type comes from
    # the card L was pressed on, not from the (Japanese) default.
    note_type = note.note_type()["name"]
    alt_target = note_type if note_type in _notetypes(cfg) else cfg["notetype"]
    # Read before the search is replaced: this is the row the way back out lands on.
    anchor = b.capture_anchor(browser)
    log(f"L: entering alternates for {word!r} from {current!r} "
        f"(anchor={anchor[:1] or None}, +{max(0, len(anchor) - 1)} neighbours)")
    # `current` is known not to be a drilled-in query — the branch above returned if it
    # was — so home can never be pinned to a view L is a way out of.
    browser._gigaku_home = current
    browser._gigaku_home_cards = anchor
    # Entering keeps whatever card Anki holds on to: it is the card L was pressed on, and
    # the one the alternates are being compared *against*. The landing is still asked for,
    # because the focus belongs on the list either way.
    _order_first(browser, "alternates")
    b.land_after(browser, b.keep_selection)
    browser.search_for(queries.alt_query(alt_target, study_field, word))


# The order used to be re-asserted 400ms after every background op, because a merge or 💣
# finishes as a CollectionOp whose refresh re-runs the search — and back then the sort had
# to be re-applied to survive that refresh. It doesn't any more: the remembered order lives
# in the table's own sort state (`b.remember_order`), which a refresh reads rather than
# resets, and the one thing that *did* overwrite it — O sorting the episode by Created —
# is removed at the source (`mvj_retunes._suppress_context_sort`). Nothing left to fight.


# ── wiring ───────────────────────────────────────────────────────────────────


def _setup_goto_shortcut():
    """One main-window key per language's queue: J → ja (the default note type), K → de —
    or, with `goto_de_query` set, K → that search (deck:"Frequency 🇩🇪" since 2026-08-26)."""
    global _goto_shortcuts
    if mw is None:
        return
    for old in _goto_shortcuts:
        try:
            old.setEnabled(False)
            old.deleteLater()
        except Exception:  # noqa: BLE001
            pass
    _goto_shortcuts = []
    cfg = _cfg()
    for key, notetype, query in (
            (cfg.get("goto_hotkey"), None, ""),
            (cfg.get("goto_de_hotkey"), cfg.get("de_notetype"),
             (cfg.get("goto_de_query") or "").strip())):
        if not key or (notetype is not None and not notetype):
            continue
        sc = QShortcut(QKeySequence(key), mw)
        sc.setContext(Qt.ShortcutContext.WindowShortcut)  # only when main window is focused
        sc.activated.connect(lambda nt=notetype, q=query: goto_browser_search(nt, q))
        _goto_shortcuts.append(sc)


def _setup_browser_shortcut(browser):
    key = _cfg().get("alternates_hotkey") or ""
    if not key or getattr(browser, "_gigaku_alt_shortcut", None) is not None:
        return
    sc = QShortcut(QKeySequence(key), browser)
    try:
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
    except Exception:  # noqa: BLE001
        pass
    sc.activated.connect(lambda br=browser: browse_alternates_toggle(br))
    browser._gigaku_alt_shortcut = sc


def install():
    gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(1000, _setup_goto_shortcut))
    gui_hooks.browser_menus_did_init.append(_setup_browser_shortcut)
    # Re-read the main window's key whenever a browser opens: the Hotkeys tab is reached
    # from there, and a key changed in it should not wait for the next Anki launch. The
    # setup replaces its own shortcut, so running it again is free.
    gui_hooks.browser_will_show.append(lambda *_: _setup_goto_shortcut())
