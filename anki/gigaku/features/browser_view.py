"""What the Browser *shows* in gigaku's views — decided once, on the search itself.

The design after presets were tried and rejected: the browser REMEMBERS the view — the
columns, their order, their widths, the sort column and its direction — exactly as the
user last shaped them by hand, and re-applies that one state on every arrival at the
my-learn queue and at L's alternates. **The episode context (MvJ's O) is the exception,
and the only one**: those cards are an episode, so they read in the order they were
watched, and that order is put on the search alone — never written to the table's sort
state, so it cannot leak into the order the other two views remember.

Everything hangs off Anki's own two search hooks, which is what makes a press cost exactly
one search:

* **`browser_will_search`** — the shape. Anki has just resolved the sort into
  `context.order` and has not run the query yet, and the whole search is bracketed by one
  model reset, so the columns and the order are set here and the query that is already
  running answers with them. (The previous design applied the sort to the table and called
  `browser.search()` again to make it take: two to four full searches of a 460k-card
  collection per keypress, and the second one is the one the user felt.)
* **`browser_did_search`** → `b.after_search` — the landing. The first moment the view is
  final: rows in place and Anki's own selection restore already done. Widths, the pending
  landing, the focus and the arrival tooltip all happen there, once.

**Capture is off the user's hands, not off the searches.** A sort is remembered when the
user *clicks a header* (`sectionClicked` — the one signal a programmatic sort does not
emit, so MvJ sorting the browser by cardDue can no longer end up as the order every gigaku
view opens in); columns and widths are remembered off the header's own move/resize
signals, debounced. Outside the three views the user's own columns come back (stashed on
first entry, in the collection config so a restart mid-view still restores them).
"""
from aqt import gui_hooks, mw
from aqt.qt import QAction, QTimer
from aqt.utils import tooltip

from .. import queries
from ..core import browser as b
from ..core import conf
from ..core.log import log

_STASH_KEY = "gigaku.stashedColumns"
_SEEDED_KEY = "gigaku.savedSearchesSeeded"
_SEEDED_KEY_V2 = "gigaku.savedSearchesSeeded.v2"

# Fields the one-shot collapse action folds in the editor: bookkeeping the add-ons and the
# CLI write, never something a human edits by hand.
_NOISY_PREFIX = "am-"
_NOISY_NAMES = ("My Run", "My Sort Key")

_GIGAKU_VIEWS = ("queue", "alternates", "context")


def _cfg():
    section = conf.section("browser_view")
    counter = conf.section("counter")
    section["notetype"] = counter["notetype"]
    section["study_field"] = counter["study_field"]
    section["ready_tag"] = conf.am_tags()["ready"]
    return section


def _plain_fields():
    """The note fields this project (and AnkiMorphs) *write*, and therefore knows carry no
    markup — the only ones whose sort may bypass Advanced Browser's HTML-stripping temp
    table. Everything else keeps Advanced Browser's own answer, slowly and identically."""
    counter = conf.section("counter")
    return frozenset({counter["counter_field"], counter["sort_field"],
                      counter["clarity_field"], counter["allcount_field"],
                      counter["study_field"], "My Run"})


def _classify(cfg, query):
    """'queue' / 'alternates' / 'context' / 'foreign' — read off the executed search.

    Every configured note type's queue and alternates count (J and K open per-language
    homes since 2026-08-06); the unscoped home shape stays recognised because the seeded
    "my-learn" saved search still carries it.
    """
    stripped = (query or "").strip()
    notetypes = conf.section("counter").get("notetypes") or [cfg["notetype"]]
    if any(queries.is_alt_query(nt, cfg["study_field"], stripped) for nt in notetypes):
        return "alternates"
    if queries.is_context_query(conf.mvj_field("sentence_audio", "Sentence Audio"), stripped):
        return "context"
    # canon() on both sides: J/K open the Browser through `search=…`, whose string Anki
    # re-quotes before this hook sees it — a byte-compare here is what once turned the
    # queue view "foreign" and let the stash overwrite the user's columns.
    mvj_query, ready = conf.mvj_browser_query(), cfg["ready_tag"]
    homes = {queries.canon(queries.home_query(mvj_query, ready))}
    homes.update(queries.canon(queries.home_query(mvj_query, ready, nt)) for nt in notetypes)
    if queries.canon(stripped) in homes:
        return "queue"
    return "foreign"


# ── reading and writing the live table ───────────────────────────────────────


def _active_columns(browser):
    table = browser.table
    if table.is_notes_mode():
        return list(browser.col.load_browser_note_columns())
    return list(browser.col.load_browser_card_columns())


def _header(browser):
    return browser.form.tableView.horizontalHeader()


def _visible_order(browser):
    """Column keys in the order the user actually sees them — a drag reorders the header
    *visually* without touching the config order, so the header is the ground truth."""
    active = _active_columns(browser)
    try:
        header = _header(browser)
        if header.count() == len(active):
            return [active[header.logicalIndex(v)] for v in range(header.count())]
    except Exception:  # noqa: BLE001
        pass
    return active


def _apply_columns(browser, wanted):
    """Set the visible columns to `wanted` — set, **in that order**; True when moved.

    Written wholesale (the same collection-config call the toggles persist through) with a
    model reset, because `toggle_column` appends at the end and order is the point. The
    reset is kept even though every caller now runs inside `browser_will_search` — i.e.
    inside the reset Anki already brackets the search with, which in principle publishes
    the change on its own. In principle: this is the shape that has actually been run
    against a real browser for months, the saving is one view rebuild on the rare arrival
    where the columns really differ, and the last thing changed here on a "the outer reset
    covers it" argument cost the user a browser that could not search at all.

    The currently-sorted column is never dropped even when the list doesn't carry it —
    pulling the sort column out from under the table is how a view ends up sorted by a
    column it no longer shows."""
    table = browser.table
    active = _active_columns(browser)
    target = list(wanted)
    keep = getattr(getattr(table, "_state", None), "sort_column", None)
    if keep and keep not in target and keep in active:
        target.append(keep)
    if active == target and _visible_order(browser) == target:
        return False
    try:
        model, state = table._model, table._state
        model.beginResetModel()
        try:
            if table.is_notes_mode():
                browser.col.set_browser_note_columns(target)
            else:
                browser.col.set_browser_card_columns(target)
            # The state caches the active list; keep the cache agreeing with the config it
            # fronts, or the next toggle would resurrect the pre-apply columns.
            cached = getattr(state, "_active_columns", None)
            if isinstance(cached, list):
                cached[:] = target
        finally:
            model.endResetModel()
    except Exception as exc:  # noqa: BLE001 — never break a search over cosmetics
        log(f"columns: apply FAILED {exc!r}")
        return False
    return True


def _apply_widths(browser, state):
    """Give every remembered column its remembered width.

    Runs after the search, not inside it: sections only exist once the model reset has
    ended, so a width set mid-search was set on a header about to be rebuilt. Qt's
    stretch-last-section would re-inflate the last column no matter what it is told, so it
    is turned off inside the gigaku views (the original setting is remembered per browser
    and put back with the user's own columns)."""
    try:
        header = _header(browser)
        if not hasattr(browser, "_gigaku_stretch_was"):
            browser._gigaku_stretch_was = bool(header.stretchLastSection())
        header.setStretchLastSection(False)
        columns = _active_columns(browser)
        for key, width in (state.get("widths") or {}).items():
            if key in columns:
                header.resizeSection(columns.index(key), int(width))
    except Exception as exc:  # noqa: BLE001
        log(f"columns: widths FAILED {exc!r}")


def _restore_stretch(browser):
    was = getattr(browser, "_gigaku_stretch_was", None)
    if was is None:
        return
    del browser._gigaku_stretch_was
    if was:
        try:
            _header(browser).setStretchLastSection(True)
        except Exception:  # noqa: BLE001
            pass


# ── capture: what the user shaped, by hand ───────────────────────────────────


def _capture_shape(browser):
    """Remember the columns and widths as the user just left them."""
    columns = _visible_order(browser)
    if not columns:
        return
    try:
        header = _header(browser)
        active = _active_columns(browser)
        widths = {}
        if header.count() == len(active):
            for logical, key in enumerate(active):
                widths[key] = int(header.sectionSize(logical))
    except Exception:  # noqa: BLE001
        widths = {}
    current = conf.view_state()
    state = dict(current, columns=columns, widths=widths or current["widths"])
    if state != current:
        conf.save_view_state(state)
        log(f"view state captured: columns={columns}")


def _capture_sort(browser):
    """Remember the order the user just *clicked* — never one an add-on set.

    Programmatic sorts don't emit `sectionClicked`, which is the whole reason the capture
    hangs off it: MvJ sorts the browser by cardDue from several of its own actions, and
    under the old capture-on-re-search that order became the one every gigaku view opened
    in until something else overwrote it."""
    table_state = getattr(browser.table, "_state", None)
    current = conf.view_state()
    state = dict(current,
                 sort=getattr(table_state, "sort_column", None) or current["sort"],
                 descending=bool(getattr(table_state, "sort_backwards", current["descending"])))
    if state != current:
        conf.save_view_state(state)
        log(f"view state captured: sort={state['sort']} desc={state['descending']}")


def _sort_clicked(browser):
    """The user clicked a header. The state hasn't moved yet — the click only asks Anki to
    sort, and that happens in the search it triggers — so this only records *that it was a
    click*, and the search that follows decides what to do with it: remembered in the queue
    and the alternates, obeyed-but-not-remembered in the episode context."""
    browser._gigaku_sort_click = True
    if _kind_of(browser) == "context":
        browser._gigaku_ctx_click = True


def _capture_soon(browser, delay_ms=600):
    """Debounced capture off the header's signals — a drag emits dozens of resizes."""
    if getattr(browser, "_gigaku_applying", False):
        return
    if getattr(browser, "_gigaku_capture_pending", False):
        return
    browser._gigaku_capture_pending = True

    def fire():
        browser._gigaku_capture_pending = False
        if _kind_of(browser) in _GIGAKU_VIEWS:
            _capture_shape(browser)

    QTimer.singleShot(delay_ms, fire)


def _wire_header(browser):
    """Header gestures don't re-run the search (a resize and a drag only move the header),
    so they are read off the header's own signals — and a *click* is the only honest
    evidence that a sort was the user's idea."""
    if getattr(browser, "_gigaku_header_wired", False):
        return
    browser._gigaku_header_wired = True
    try:
        header = _header(browser)
        header.sectionResized.connect(lambda *_: _capture_soon(browser))
        header.sectionMoved.connect(lambda *_: _capture_soon(browser))
        header.sectionCountChanged.connect(lambda *_: _capture_soon(browser))
        header.sectionClicked.connect(lambda *_: _sort_clicked(browser))
    except Exception as exc:  # noqa: BLE001
        log(f"header wiring FAILED {exc!r}")


# ── the search hooks ─────────────────────────────────────────────────────────


def _kind_of(browser):
    at = getattr(browser, "_gigaku_at", None)
    return at[0] if at else _classify(_cfg(), b.current_search_text(browser))


def _order_by_state(context, browser):
    """Sort this search by whatever the table's sort state names — cheaply, when we can.

    Keyed on the *state* rather than on the remembered order, so a column the user just
    clicked gets the fast path too, and so this can run on a re-search without overriding
    what they chose. Returns True when the order was put on the search; False leaves it as
    Anki resolved it, i.e. Advanced Browser's temp table, which is slow and always right."""
    state = getattr(browser.table, "_state", None)
    key = getattr(state, "sort_column", None)
    if not key:
        return False
    sql = b.field_order_sql(key, descending=bool(getattr(state, "sort_backwards", False)),
                            plain_only=_plain_fields())
    return bool(sql) and b.order_search_sql(context, sql)


def _on_will_search(context):
    """Shape the view before the query runs — one search, in the right order, with the
    right columns."""
    browser = getattr(context, "browser", None)
    if browser is None or mw is None or mw.col is None:
        return
    _wire_header(browser)
    kind = _classify(_cfg(), getattr(context, "search", None))
    at = (kind, getattr(context, "search", None), b.notes_mode(browser))
    arrival = getattr(browser, "_gigaku_at", None) != at
    browser._gigaku_at = at
    browser._gigaku_arrival = arrival
    if arrival:
        # A sort the user clicked inside the episode context belongs to the visit, not to
        # the view: leaving it is what ends it.
        browser._gigaku_ctx_click = False

    if kind == "foreign":
        _restore_stretch(browser)
        stash = mw.col.get_config(_STASH_KEY, None)
        if stash:
            mw.col.set_config(_STASH_KEY, None)
            browser._gigaku_applying = True
            try:
                if _apply_columns(browser, stash):
                    log("columns: user's own restored")
            finally:
                browser._gigaku_applying = False
        return

    sort_key, descending = conf.view_sort(kind)

    if kind != "context" and not arrival:
        # A re-search of a view already in place — an op's refresh, the user's own sort
        # click. Whatever the table is sorted by *now* is right; it just must not cost two
        # seconds of temp table to say so again.
        _order_by_state(context, browser)

    if kind == "context":
        # **Every** search of the episode view, not only the arrival: its order is carried
        # by the search rather than by the sort state, so a re-search Anki runs on its own
        # (a background op's refresh, a reload) would otherwise fall back to the remembered
        # order and quietly take the episode out of episode order. The exception is a sort
        # the user clicked here — overriding that would make the click do nothing.
        if not getattr(browser, "_gigaku_ctx_click", False):
            b.order_search(context, sort_key, descending=descending)

    if not arrival:
        # A re-search inside the same view — a sort click, a background op's refresh, a
        # reload: the view is the user's now, and nothing here second-guesses it.
        return

    if mw.col.get_config(_STASH_KEY, None) is None:
        mw.col.set_config(_STASH_KEY, _active_columns(browser))
    browser._gigaku_applying = True
    try:
        _apply_columns(browser, conf.view_state()["columns"])
        if kind != "context":
            moved = b.remember_order(browser, sort_key, descending=descending)
            # The state now names the order this view wants; sorting by it here costs a
            # field read per result row instead of Advanced Browser's whole-collection temp
            # table — and it lands on *this* search, so a moved state needs no re-run.
            if _order_by_state(context, browser):
                moved = False
        else:
            moved = False
        if moved:
            # The state had drifted (MvJ sorts by cardDue from several of its own actions;
            # so does a click in a view that doesn't remember one) and this search already
            # resolved its order from the old value — `_search_inner` does that before any
            # hook runs. It cannot be corrected *on* this search: the remembered order is
            # an Advanced Browser column, and only Advanced Browser can turn one into
            # something the backend will sort by. So the corrected state gets one re-run,
            # once this search is done, and the presses that know where they are going set
            # the state *before* searching (`nav`, `mvj_retunes`) so they never come here.
            browser._gigaku_reorder = True
            log(f"view: order corrected to {sort_key} — re-running the search once")
    finally:
        browser._gigaku_applying = False
    log(f"view: arrived at {kind} — order {sort_key} "
        f"{'desc' if descending else 'asc'}{' (on the search only)' if kind == 'context' else ''}")


def _on_did_search(context):
    """Finish the arrival the moment the search is final — widths, landing, focus."""
    browser = getattr(context, "browser", None)
    if browser is None:
        return
    at = getattr(browser, "_gigaku_at", None)
    kind = at[0] if at else "foreign"
    arrival = bool(getattr(browser, "_gigaku_arrival", False))
    clicked = getattr(browser, "_gigaku_sort_click", False)
    browser._gigaku_sort_click = False
    b.after_search(lambda: _settled(browser, kind, arrival, clicked))


def _settled(browser, kind, arrival, clicked):
    """The view is final: rows in place, Anki's own selection restore already done."""
    if kind in _GIGAKU_VIEWS and arrival:
        browser._gigaku_applying = True
        try:
            _apply_widths(browser, conf.view_state())
            if kind == "context":
                b.hide_sort_indicator(browser)
        finally:
            browser._gigaku_applying = False
    # A click inside the episode context sorts that view and is not remembered: its order
    # is the episode's, and the other two views keep the order the user gave them.
    if clicked and kind in ("queue", "alternates"):
        _capture_sort(browser)
    if arrival:
        _feedback(browser, kind)
    if getattr(browser, "_gigaku_reorder", False):
        # The sort state was corrected during this search, too late for it. Run it again —
        # `_search_inner` resolves the order from the state, and whoever owns that column
        # transforms it on the way, exactly as it does for a header click. The landing is
        # deliberately left pending: it belongs to the re-sorted view, not this one.
        browser._gigaku_reorder = False
        browser.search()
        return
    land = b.take_landing(browser)
    if land is not None:
        land(browser)
        b.focus_card_list(browser)


def _feedback(browser, kind):
    """One quiet line naming the view just arrived at — the feedback channel for J/L.

    Only on a *transition*, and only once the search is finished: the count it quotes is
    read off the table, which mid-search still holds the previous view's rows."""
    try:
        n = browser.table.len()
        if kind == "queue":
            tooltip(f"my-learn · {n:,} words")
        elif kind == "alternates":
            word = _word_of(browser, _cfg())
            tooltip(f"alternates of {word} · {n:,} cards" if word else f"alternates · {n:,} cards")
    except Exception:  # noqa: BLE001
        pass


def _word_of(browser, cfg):
    from .. import rules

    try:
        cids = browser.table.get_selected_card_ids()
        if not cids:
            return ""
        note = mw.col.get_card(cids[0]).note()
        if cfg["study_field"] not in note:
            return ""
        return rules.norm_morph(note[cfg["study_field"]])
    except Exception:  # noqa: BLE001
        return ""


# ── saved searches ───────────────────────────────────────────────────────────


def _seed_saved_searches():
    """Put the working searches in the sidebar, once ever — v2 runs once more.

    Seeded behind a one-time marker rather than re-asserted: a user who deletes one has
    decided, and a feature that re-adds it every start is fighting them.

    v2 (2026-08-08): the live collection's `my-learn` was seeded before `home_query`
    grew its note-type clause, so clicking it produced a recognised "queue" view with
    8,160 Japanese and 768 German notes mixed — the exact thing J/K exist to prevent —
    and the v1 marker guaranteed nothing would ever fix it. The stale value is updated
    ONLY while it still canon-equals what v1 wrote (recomputed at runtime — the
    `conf.unseed_hotkeys` pattern: a row the user edited is never fought), the German
    queue and i+1 entries are setdefault'ed, and both markers are set."""
    try:
        if mw.col.get_config(_SEEDED_KEY_V2, False):
            return
        cfg = _cfg()
        ready = cfg["ready_tag"]
        de_nt = conf.section("nav").get("de_notetype", "🇩🇪 German")
        saved = mw.col.get_config("savedFilters", {})

        v1_shape = queries.home_query(conf.mvj_browser_query(), ready)  # unscoped
        scoped = queries.home_query(conf.mvj_browser_query(), ready, cfg["notetype"])
        if queries.canon(saved.get("my-learn", "")) == queries.canon(v1_shape):
            saved["my-learn"] = scoped
        saved.setdefault("my-learn", scoped)
        saved.setdefault("i+1 all", queries.all_query(cfg["notetype"], ready))
        saved.setdefault("my-learn 🇩🇪",
                         queries.home_query(conf.mvj_browser_query(), ready, de_nt))
        saved.setdefault("i+1 all 🇩🇪", queries.all_query(de_nt, ready))

        mw.col.set_config("savedFilters", saved)
        mw.col.set_config(_SEEDED_KEY, True)
        mw.col.set_config(_SEEDED_KEY_V2, True)
        log("saved searches seeded (my-learn, i+1 all — 🇯🇵 and 🇩🇪)")
    except Exception as exc:  # noqa: BLE001
        log(f"saved searches FAILED {exc!r}")


# ── the one-shot collapse action ─────────────────────────────────────────────


def _collapse_noisy_fields():
    """Fold the bookkeeping fields in the editor, so a card opens showing what a human
    edits. Explicit menu action, never automatic — it writes the notetype — and each field
    unfolds again with one click on its own arrow in the editor."""
    cfg = _cfg()
    # Every note type of ours, not just the Japanese one — the same audit that fixed M's
    # word sweep (2026-08-07): a singular-notetype read leaves German's bookkeeping
    # fields unfolded.
    notetypes = conf.section("counter").get("notetypes") or [cfg["notetype"]]
    changed = []
    for name in notetypes:
        model = mw.col.models.by_name(name)
        if not model:
            tooltip(f'No notetype "{name}"')
            continue
        noisy = [f for f in model["flds"]
                 if f["name"].startswith(_NOISY_PREFIX) or f["name"] in _NOISY_NAMES]
        fresh = [f["name"] for f in noisy if not f.get("collapsed")]
        if not fresh:
            continue
        for field in noisy:
            field["collapsed"] = True
        mw.col.models.update_dict(model)
        changed += fresh
    if not changed:
        tooltip("Nothing to collapse — already folded")
        return
    tooltip(f"Collapsed {len(changed)} fields: {', '.join(sorted(set(changed)))}")


def _add_menu():
    try:
        action = QAction("Gigaku: collapse noisy MvJ fields", mw)
        action.triggered.connect(_collapse_noisy_fields)
        mw.form.menuTools.addAction(action)
    except Exception as exc:  # noqa: BLE001
        log(f"collapse menu FAILED {exc!r}")


def _on_profile_open():
    _seed_saved_searches()


def install():
    # First on the hook, deliberately — see `b.hook_first`. Anything that shapes a search
    # after Advanced Browser has looked at it has already paid for its temp table.
    b.hook_first(gui_hooks.browser_will_search, _on_will_search)
    gui_hooks.browser_did_search.append(_on_did_search)
    gui_hooks.profile_did_open.append(_on_profile_open)
    gui_hooks.main_window_did_init.append(_add_menu)
