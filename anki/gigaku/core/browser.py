"""Browser helpers shared by nav, keys and the retunes — **one search per press**.

Every gigaku navigation is the same three things: run a search, shape the view it lands in
(columns, order), put the selection and the focus somewhere. This module holds the
primitives; `features/browser_view.py` is the one place that decides *what* a view looks
like, off Anki's own search hooks.

What this replaced, and why. The old shape was apply-then-check: set the sort, call
`browser.search()` again to make it take, then re-assert the selection and the focus on a
ladder of timers (120/250/500ms) until the same card came up twice running. It worked
because it out-waited whatever else the browser was doing, and it cost what you would
expect — two to four full searches of a 460k-card collection per keypress, and half a
second of settling on top. Both are gone, because Anki hands out the two moments that
actually matter:

* **`browser_will_search`** fires after Anki has resolved the sort into `context.order`
  and before the query runs (`DataModel._search_inner`), inside the model reset that
  brackets the whole search. So the order and the columns are set *on the search that is
  already running*: one query, in the right order, with the right columns, and nothing to
  re-assert afterwards.
* **`after_search`** (a zero-delay timer) is the first moment the view is final. Zero is
  not a guess and not a delay — Qt runs it on the next turn of the event loop, i.e. the
  moment the whole synchronous search stack has unwound: `Browser.search` → `Table.search`
  → model reset → `_restore_selection`. Anki restoring *its* remembered selection is the
  last thing in that stack, which is exactly what used to overwrite a landing fired 100ms
  in and made the return from `O` a coin flip.

The one thing still read back rather than trusted is a selected row: `select_row` reports
which card actually ended up selected, because that is the only way to tell "the row is
selected" from "the table swapped underneath us".
"""
from aqt import dialogs
from aqt.qt import QTimer

from .log import log


def open_browser():
    """The open Browser, or None."""
    entry = dialogs._dialogs.get("Browser")
    return entry[1] if entry else None


def browser_open():
    return open_browser() is not None


def current_search_text(browser):
    """The search that is actually *running*, not whatever is typed in the bar.

    Anki keeps the executed query in `_lastSearchTxt` (set by `Browser.search_for`); the
    line edit can hold text the user typed and never entered. Remembering that instead
    would re-run a query the user never ran — and `Browser.search` answers a query it
    can't parse with a modal `showWarning`, which is one of the ways focus ends up off the
    card list."""
    last = getattr(browser, "_lastSearchTxt", None)
    if isinstance(last, str) and last.strip():
        return last
    try:
        return browser.form.searchEdit.lineEdit().text()
    except Exception:  # noqa: BLE001
        try:
            return browser.current_search()
        except Exception:  # noqa: BLE001
            return ""


def searchbar_text(browser):
    try:
        return browser.form.searchEdit.lineEdit().text()
    except Exception:  # noqa: BLE001
        return ""


def notes_mode(browser):
    try:
        return bool(browser.table.is_notes_mode())
    except Exception:  # noqa: BLE001
        return False


def focus_card_list(browser):
    """Leave keyboard focus on the card list, with a current row to move from.

    The list keys are bound to the table view, so anything that ends with focus elsewhere
    leaves them dead until a row is clicked. Row first, then focus (the two old copies
    disagreed on the order): picking the fallback row before `onCardList` means focus
    lands on a list that already has a current row, instead of the focus landing first and
    the row appearing under it a paint later."""
    try:
        # Opening the browser on a specific card already selects it; only fall back to the
        # first row when the search left no current row.
        if browser.table.len() and not browser.table.has_current():
            browser.table.to_first_row()
    except Exception as exc:  # noqa: BLE001
        log(f"focus: row fallback FAILED {exc!r}")
    try:
        browser.onCardList()  # Anki's own "focus the card list"
    except Exception:  # noqa: BLE001
        try:
            browser.form.tableView.setFocus()
        except Exception as exc:  # noqa: BLE001
            log(f"focus: setFocus FAILED {exc!r}")


# Column keys **this add-on may put on a search itself**. Everything else goes through the
# sort state and is left to whoever owns it.
#
# The distinction is not pedantry, it is the bug that shipped. `browser_will_search` is a
# *shared* hook: Advanced Browser is on the same one, and for its own `_field_<name>`
# columns it replaces `context.order` with **raw SQL** (`advancedbrowser/core.py`,
# `willSearch`) because the backend cannot sort by a note field at all — it registers those
# columns as `SORTING_NONE`. Setting `context.order` to such a column *after* Advanced
# Browser has run throws its SQL away and hands the backend a column it has just been told
# is unsortable, so **every** search dies with "Can't sort Cards by Custom" — which is
# exactly what it did. Anki's own builtins have no such owner: `noteCrt` (the episode
# order) is not among the keys Advanced Browser claims, so it is safe from either side of
# the hook, in any load order.
SAFE_ORDER_KEYS = frozenset({
    "noteCrt", "noteMod", "noteFld", "cardMod", "cardDue", "cardIvl", "cardReps",
    "cardLapses",
})


def order_search(context, col_key, *, descending=False):
    """Run **this** search in `col_key` order — set on the search, so it costs nothing.

    `DataModel._search_inner` resolves `context.order` from the table's sort column just
    *before* `browser_will_search` and passes it to `find_items`, so a hook can replace it
    and the query that is already running answers in the order we asked for. Nothing is
    written and nothing is re-run.

    Only for a key in `SAFE_ORDER_KEYS` — read its comment before adding one. The order has
    to be a real `Column` (pylib reads a plain string as raw SQL), and the model holds every
    column Anki knows, keyed: the same dict `_search_inner` itself indexes."""
    if col_key not in SAFE_ORDER_KEYS:
        log(f"order: {col_key} is not ours to put on a search — left to the sort state")
        return False
    try:
        context.order = context.browser.table._model.columns[col_key]
        context.reverse = bool(descending)
        return True
    except Exception as exc:  # noqa: BLE001 — never break a search over a sort
        log(f"order: {col_key} not available ({exc!r})")
        return False


FIELD_PREFIX = "_field_"          # Advanced Browser's note-field columns
_verified_sql = {}


def field_order_sql(col_key, *, descending=False, plain_only=()):
    """ORDER BY for a note-field column, computed in SQL — **without the temp table**.

    This is the whole remaining cost of a press, measured: a search of the my-learn queue
    took **2.25s**, of which the backend's own find was ~0.1s. The other two seconds are
    Advanced Browser. The backend cannot sort by a note field, so for every search ordered
    by one of its `_field_*` columns it rebuilds a temp table from scratch — `select id,
    field_at_index(flds, ord) from notes where mid = …` for **every note of the notetype**
    (462,487 of them here), each one HTML-stripped in Python and re-inserted — and then
    sorts through a correlated subquery against it. On every search. Nothing about that is
    incremental, and none of it depends on the search.

    The field can be read straight out of the note row instead, with the same
    `field_at_index` the temp table is filled from, so the sort costs a field extraction per
    *result* row and nothing per collection row. The expression mirrors Advanced Browser's
    exactly — numeric-when-numeric, `collate nocase`, empties last — so the order the user
    sees does not change. A string `context.order` is raw SQL to pylib (`_build_sort_mode`),
    which is what Advanced Browser passes too, and its hook only rewrites a `BuiltinColumn`,
    so ours goes through untouched from either side of the hook. **pylib ignores `reverse`
    for a custom order** — hence the direction baked in, the same way Advanced Browser bakes
    it in.

    `plain_only` is the guard on the one place the two can differ: Advanced Browser strips
    HTML before sorting and this does not, so it is offered only for fields *this project
    writes*, which are plain text by construction. A field with markup in it (MvJ's
    Sentence, Word) is left to Advanced Browser, slow and identical.
    """
    if not col_key.startswith(FIELD_PREFIX):
        return None
    name = col_key[len(FIELD_PREFIX):]
    if name not in plain_only:
        return None
    try:
        from aqt import mw

        pairs = sorted({(int(notetype["id"]), int(field["ord"]))
                        for notetype in mw.col.models.all()
                        for field in notetype["flds"] if field["name"] == name})
    except Exception as exc:  # noqa: BLE001
        log(f"order: field ordinals for {name!r} FAILED {exc!r}")
        return None
    if not pairs:
        return None
    if len(pairs) == 1:
        value = f"field_at_index(n.flds, {pairs[0][1]})"
    else:
        # The same field name can sit at a different ordinal in another notetype, which is
        # why the temp table is keyed by note id at all.
        cases = " ".join(f"when {mid} then field_at_index(n.flds, {ord_})" for mid, ord_ in pairs)
        value = f"case n.mid {cases} end"
    value = f"nullif({value}, '')"
    direction = "desc" if descending else "asc"
    return (f"case when {value} glob '*[^0-9.]*' then {value} else cast({value} as real) end "
            f"collate nocase {direction} nulls last")


def hook_first(hook, callback):
    """Register `callback` **ahead of** everything already on `hook`.

    For `browser_will_search` the position is the whole point, not a preference. Advanced
    Browser builds its temp table the moment it sees one of its own columns in
    `context.order` — before it rewrites it — so replacing that order afterwards skips the
    rewrite but pays for the table anyway: measured, the two seconds stayed. Going first
    means it sees a plain SQL string instead, `type(ctx.order) == BuiltinColumn` is false,
    and it does nothing at all. When we hand it a column it *does* own (a field we cannot
    prove is plain text) it still runs and still builds the table, which is the slow,
    correct fallback.

    `_hooks` is a plain list on the hook object, and `append` is a one-line wrapper over
    it; if a future Anki hides it, appending still leaves everything working, only slow."""
    try:
        hook._hooks.insert(0, callback)
    except Exception as exc:  # noqa: BLE001
        log(f"hook: could not register first ({exc!r}) — appending instead")
        hook.append(callback)


def order_search_sql(context, sql):
    """Put hand-written ORDER BY SQL on this search — once it has been proven to run.

    The proof is not optional. Bad SQL here fails at prepare time, i.e. **every** search in
    the browser dies with a modal — which is exactly the shape of the last bug this file
    shipped, from the other direction. So the first use of a given expression runs it
    against a search that can match at most one card; if that raises, the expression is
    marked bad for the session and the caller falls back to the sort state, which is the
    slow path but always works."""
    verdict = _verified_sql.get(sql)
    if verdict is None:
        try:
            from aqt import mw

            mw.col.find_cards("cid:1", order=sql)
            verdict = True
        except Exception as exc:  # noqa: BLE001
            log(f"order: hand-written SQL refused, falling back ({exc!r})\n  {sql}")
            verdict = False
        _verified_sql[sql] = verdict
    if not verdict:
        return False
    context.order = sql
    return True


def remember_order(browser, col_key, *, descending=False):
    """Put `col_key` in the table's own sort state — no search, and no write unless it
    actually moves. Returns True if it moved.

    **This, not `order_search`, is how a view gets an order it does not own** (`_field_*`
    belongs to Advanced Browser): the state is what `_search_inner` resolves from at the
    top of a search, before any add-on's hook, so setting it and *then* searching gets
    every owner's transformation applied for free — it is the same path a header click
    takes. Called before `search_for` a press costs nothing extra; called from inside a
    search it only takes effect on the next one, which is why `browser_view` re-runs that
    one search when it has to correct an arrival nobody prepared.

    It also puts the arrow on the right header, and makes the order stick for the searches
    Anki runs by itself (a background op's refresh, a Cards/Notes toggle). The setter
    persists to the collection config, so it is only ever called for a view whose order the
    user is entitled to have remembered — never for the episode context, which carries its
    order on the search alone."""
    try:
        state = browser.table._state
        moved = False
        if getattr(state, "sort_column", None) != col_key:
            state.sort_column = col_key
            moved = True
        if bool(getattr(state, "sort_backwards", False)) != bool(descending):
            state.sort_backwards = bool(descending)
            moved = True
        if moved:
            browser.table._set_sort_indicator()
        return moved
    except Exception as exc:  # noqa: BLE001
        log(f"order: remembering {col_key} FAILED {exc!r}")
        return False


def hide_sort_indicator(browser):
    """Take the arrow off the header while the rows are not in the header's order.

    The episode context orders the search without touching the sort state, so the state
    still names the remembered column — and an arrow over a column the rows are not sorted
    by is a lie the user would reasonably act on."""
    try:
        browser.form.tableView.horizontalHeader().setSortIndicatorShown(False)
    except Exception:  # noqa: BLE001 — cosmetic
        pass


def select_row(browser, row):
    """Select a row by index and report which card actually ended up selected.

    Read back rather than trusted, because that is the only way to tell "the row is
    selected" from "the table swapped underneath us"."""
    try:
        table = browser.table
        if row < 0 or row >= table.len():
            return None
        table._move_current_to_row(row)
        cids = table.get_selected_card_ids()
        return cids[0] if len(cids) == 1 else None
    except Exception as exc:  # noqa: BLE001
        log(f"row {row} FAILED {exc!r}")
        return None


def select_top(browser):
    """Land on the first row — the highest sort key, i.e. the next word to study."""
    return select_row(browser, 0)


# How many rows either side of the anchor are remembered as fallbacks, for when the
# anchored card itself is gone from the view it was taken from.
ANCHOR_NEIGHBOURS = 5


def card_at_row(browser, row):
    """The card id at a row, in Cards *or* Notes mode.

    Asked through the model's own item→card mapping rather than read off ``_items``,
    because in Notes mode a row **is a note**: recording note ids and looking them up
    later would break the moment the mode changed, and MvJ's ``O`` switches the browser to
    Cards mode on its way down."""
    model = browser.table._model
    try:
        # Checked rather than trusted: an invalid index reads `_items[-1]`, i.e. the
        # *last* row silently answering for the one asked about — which the model does
        # while a backend operation blocks it.
        index = model.index(row, 0)
        if not index.isValid():
            return None
        cids = model.get_card_ids([index])
    except Exception as exc:  # noqa: BLE001
        log(f"anchor: row {row} yielded no card ({exc!r})")
        return None
    return cids[0] if cids else None


def capture_anchor(browser):
    """The card the view is focused on, then its neighbours — where a return will land.

    The **current** row, not the first selected one: what has to be given back is where
    the focus was, and with a multi-row selection those are different rows. Neighbours are
    recorded because the anchored card routinely isn't there any more on the way back —
    the everyday round trip ends with its word marked known, which drops it out of the
    my-learn view it was picked from. **Downwards first**: the row that took its place is
    the next word to study, whereas the row above is the word before it, done with."""
    try:
        table = browser.table
        rows = table.len()
        row = table._current().row() if table.has_current() else -1
        if not (0 <= row < rows):
            return []
        near = [row]
        near += range(row + 1, min(rows, row + 1 + ANCHOR_NEIGHBOURS))
        near += range(row - 1, max(-1, row - 1 - ANCHOR_NEIGHBOURS), -1)
        return [cid for cid in (card_at_row(browser, r) for r in near) if cid]
    except Exception as exc:  # noqa: BLE001
        log(f"anchor: capture FAILED {exc!r}")
        return []


def select_anchor(cards):
    """A landing that goes back to the anchored card, or its nearest surviving neighbour.

    Cards are found by id in whatever the view holds *now*, which is what makes this
    mode-proof: ``get_card_row`` maps a card to its note's row in Notes mode, so the round
    trip survives MvJ's ``O`` switching the browser to Cards mode in between. With none of
    the remembered stretch left this is no longer the same list — a re-typed search, a
    recalc that reshuffled everything — and the top row is then a better answer than a
    guessed row index, since it is where every other arrival lands."""

    def land(browser):
        model = browser.table._model
        for offset, cid in enumerate(cards):
            try:
                row = model.get_card_row(cid)
            except Exception as exc:  # noqa: BLE001
                log(f"anchor: lookup of card {cid} FAILED {exc!r}")
                continue
            if row is None:
                continue
            landed = select_row(browser, row)
            if landed is not None:
                if offset:
                    log(f"anchor: card gone — landed on neighbour #{offset} (row {row})")
                return landed
        log("anchor: nothing left of the remembered rows — top row instead")
        return select_top(browser)

    return land


def keep_selection(browser):
    """A landing that lands nowhere: focus the list, leave the selection alone.

    Entering the alternates wants exactly this — Anki's own selection restore already
    holds the card `L` was pressed on, and it is the card the alternates are being
    compared against."""
    return None


# ── the landing: one intent, consumed by the search it was set for ───────────


def land_after(browser, land):
    """Ask the next search to end on `land` (a `select_*` callable) with focus on the list.

    A press sets this immediately before calling `search_for`, and the search consumes it —
    once. Everything else the browser searches for on its own (a background op's refresh, a
    sort click, a sidebar pick) finds no intent and is left exactly as the user left it,
    which is the rule the old timer-driven resort had to state as a special case."""
    browser._gigaku_land = land


def take_landing(browser):
    """The pending landing, cleared. A second search can't re-land the first one's."""
    land = getattr(browser, "_gigaku_land", None)
    browser._gigaku_land = None
    return land


def after_search(action):
    """Run `action` the moment the running search is finished, and not a moment later.

    `singleShot(0)` is an ordering primitive, not a delay: Qt runs it on the next turn of
    the event loop, i.e. as soon as the synchronous search stack unwinds — model reset
    ended, rows in place, and Anki's own `_restore_selection` (the last thing in that
    stack, and the thing that used to overwrite our landing) already done."""
    QTimer.singleShot(0, action)
