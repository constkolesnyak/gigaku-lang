"""A stubbed Anki, just real enough to drive the add-on's browser navigation.

Run as a script; prints one JSON object of what was observed. `test_addon_browser.py` runs
it in a subprocess (the stubs go in `sys.modules` and importing the add-on installs its
hooks, neither of which belongs in the session every other test shares) and asserts on the
result.

**The stub's fidelity is the whole point, so every load-bearing detail here was read off
Anki's own bytecode** (`aqt/browser/table/model.pyc`, `state.pyc`, `table.pyc`) rather than
assumed:

* `DataModel._search_inner` resolves `context.order` from the table's sort column **before**
  `browser_will_search` and passes it to `find_items` — which is what lets a hook set the
  order of the search that is already running, instead of sorting and searching again.
* `ItemState.sort_column`/`sort_backwards` are properties whose setters **write the
  collection config**. That is why the episode view must not use them, and the stub records
  every write so the test can prove it doesn't.
* `Table.search` restores the selection Anki remembers **after** the model is done, i.e.
  after `browser_did_search`. That is the thing that used to overwrite a landing fired from
  a timer, so the stub does it in that order too.
* **Advanced Browser is on the same hook**, and it is simulated here because leaving it out
  is what let a version of this ship that broke every search in the browser with "Can't
  sort Cards by Custom". It registers a column per note field (`_field_<name>`) as
  *unsortable* and then, in its own `browser_will_search`, replaces `context.order` with
  raw SQL — the backend cannot sort by a note field otherwise. Its hook is registered
  first here, as it is in Anki (add-ons load by folder name, and `874215009` sorts before
  `gigaku`), so anything that overwrites `context.order` afterwards throws that SQL away
  and hands the backend the unsortable column. `find_items` below raises exactly as the
  backend does when that happens.
"""
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "anki"))


# ── the Qt/aqt surface the add-on touches ────────────────────────────────────

timers = []
tooltips = []


class QTimer:
    @staticmethod
    def singleShot(ms, fn):
        timers.append((ms, fn))


def run_timers():
    """Drain the queue the way the event loop would once the stack unwinds."""
    while timers:
        timers.pop(0)[1]()


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class Hook(list):
    """`_BrowserWillSearchHook`: a plain `_hooks` list behind `append`/`__call__`, which
    is what lets a callback register *ahead* of Advanced Browser's."""

    @property
    def _hooks(self):
        return self

    def __call__(self, *args):
        for fn in list(self):
            fn(*args)


def _install_aqt():
    aqt = types.ModuleType("aqt")
    aqt.qt = types.ModuleType("aqt.qt")
    aqt.qt.QTimer = QTimer
    aqt.qt.QAction = type("QAction", (), {
        "__init__": lambda self, *a: setattr(self, "triggered", Signal())})
    for name in ("QKeySequence", "QShortcut", "Qt", "QEvent", "QObject", "QBrush",
                 "QColor", "QPoint", "QPointF"):
        setattr(aqt.qt, name, object)
    aqt.utils = types.ModuleType("aqt.utils")
    aqt.utils.tooltip = lambda text, **kw: tooltips.append(text)
    aqt.gui_hooks = types.ModuleType("aqt.gui_hooks")
    for name in ("browser_will_search", "browser_did_search", "profile_did_open",
                 "main_window_did_init", "browser_menus_did_init", "browser_will_show",
                 "operation_did_execute"):
        setattr(aqt.gui_hooks, name, Hook())
    aqt.dialogs = types.SimpleNamespace(_dialogs={})
    aqt.browser = types.ModuleType("aqt.browser")
    aqt.browser.Browser = type("Browser", (), {})
    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = aqt.qt
    sys.modules["aqt.utils"] = aqt.utils
    sys.modules["aqt.gui_hooks"] = aqt.gui_hooks
    sys.modules["aqt.browser"] = aqt.browser
    return aqt


aqt = _install_aqt()


# ── the collection and the browser ───────────────────────────────────────────


class Models:
    """Just enough of `col.models` to answer "where does that field live"."""

    NOTETYPES = [
        {"id": 1690000000000, "name": "🇯🇵 MvJ", "flds": [
            {"name": "Sentence", "ord": 0}, {"name": "Word", "ord": 1},
            {"name": "am-study-morphs", "ord": 2}, {"name": "am-all-morphs-count", "ord": 3},
            {"name": "My Alternatives", "ord": 4}, {"name": "My Clarity", "ord": 5},
            {"name": "My Sort Key", "ord": 6}, {"name": "My Run", "ord": 7}]},
        # A second notetype carrying the same field at a *different* ordinal — the reason
        # Advanced Browser's temp table is keyed by note id in the first place.
        {"id": 1700000000000, "name": "Other", "flds": [
            {"name": "Front", "ord": 0}, {"name": "My Sort Key", "ord": 1}]},
    ]

    def all(self):
        return [dict(n) for n in self.NOTETYPES]

    def by_name(self, name):
        return next((dict(n) for n in self.NOTETYPES if n["name"] == name), None)


class Col:
    def __init__(self):
        self.conf = {}
        self.writes = []          # every config key written, in order
        self.card_columns = ["noteFld", "cardDue"]
        self.note_columns = ["noteFld"]
        self.models = Models()
        self.preflights = []

    def find_cards(self, query, order=False, reverse=False):
        """The one-card search `order_search_sql` proves an expression with."""
        self.preflights.append(order)
        return []

    def get_config(self, key, default=None):
        return self.conf.get(key, default)

    def set_config(self, key, value):
        self.writes.append(key)
        self.conf[key] = value

    def set_config_bool(self, key, value):
        self.set_config(key, value)

    def load_browser_card_columns(self):
        return list(self.card_columns)

    def load_browser_note_columns(self):
        return list(self.note_columns)

    def set_browser_card_columns(self, columns):
        self.card_columns = list(columns)

    def set_browser_note_columns(self, columns):
        self.note_columns = list(columns)


class Header:
    def __init__(self):
        self.sectionResized = Signal()
        self.sectionMoved = Signal()
        self.sectionCountChanged = Signal()
        self.sectionClicked = Signal()
        self.widths = {}
        self.indicator_shown = True
        self.stretch = True
        self.sections = 0

    def count(self):
        return self.sections

    def logicalIndex(self, visual):
        return visual

    def sectionSize(self, index):
        return self.widths.get(index, 100)

    def resizeSection(self, index, width):
        self.widths[index] = width

    def setSortIndicatorShown(self, shown):
        self.indicator_shown = shown

    def stretchLastSection(self):
        return self.stretch

    def setStretchLastSection(self, value):
        self.stretch = value


class State:
    """`ItemState`: the sort is a property pair whose setters persist to the config."""

    def __init__(self, col):
        self.col = col
        self._sort_column = "_field_My Sort Key"
        self._sort_backwards = True

    @property
    def sort_column(self):
        return self._sort_column

    @sort_column.setter
    def sort_column(self, key):
        self.col.set_config("sortType", key)
        self._sort_column = key

    @property
    def sort_backwards(self):
        return self._sort_backwards

    @sort_backwards.setter
    def sort_backwards(self, value):
        self.col.set_config("sortBackwards", value)
        self._sort_backwards = value


class Column:
    """`BuiltinColumn`: what `_search_inner` puts in `context.order`."""

    def __init__(self, key, sortable=True):
        self.key = key
        self.sortable = sortable

    def __str__(self):
        return f"Column:{self.key}"


class Model:
    resets = 0

    def beginResetModel(self):
        Model.resets += 1

    def endResetModel(self):
        pass

    def __init__(self):
        self.columns = {key: Column(key) for key in ("noteCrt", "cardDue", "noteFld")}
        # Advanced Browser's own, registered as unsortable — it sorts them with SQL instead.
        for key in ("_field_My Sort Key", "_field_am-study-morphs", "_field_Word",
                    "_field_My Clarity"):
            self.columns[key] = Column(key, sortable=False)
        self.rows = []

    def get_card_row(self, cid):
        return self.rows.index(cid) if cid in self.rows else None


class Table:
    def __init__(self, browser, col):
        self.browser = browser
        self._state = State(col)
        self._model = Model()
        self.notes = False
        self.current = None

    def is_notes_mode(self):
        return self.notes

    def len(self):
        return len(self._model.rows)

    def has_current(self):
        return self.current is not None

    def to_first_row(self):
        self.current = self._model.rows[0] if self._model.rows else None

    def _move_current_to_row(self, row):
        self.current = self._model.rows[row]

    def get_selected_card_ids(self):
        return [self.current] if self.current is not None else []

    def _set_sort_indicator(self):
        self.browser.header.setSortIndicatorShown(True)


class Browser:
    def __init__(self, col):
        self.col = col
        self.table = Table(self, col)
        self.header = Header()
        self._lastSearchTxt = ""
        self._line = types.SimpleNamespace(text=lambda: self._lastSearchTxt,
                                           setText=lambda text: None)
        self.form = types.SimpleNamespace(
            tableView=types.SimpleNamespace(horizontalHeader=lambda: self.header,
                                            setFocus=lambda: self.focus("table")),
            searchEdit=types.SimpleNamespace(lineEdit=lambda: self._line))
        self.focused = None
        self.searches = []
        self.failures = []

    def focus(self, what):
        self.focused = what

    def onCardList(self):
        self.focus("table")

    def search_for(self, text, rows=None, remembers=None):
        self._lastSearchTxt = text
        self.search(rows=rows, remembers=remembers)

    def search(self, rows=None, remembers=None):
        """`Browser.search` → `Table.search`, in Anki's order."""
        context = types.SimpleNamespace(
            search=self._lastSearchTxt, browser=self, ids=None,
            # resolved from the sort state *before* the hook — see the module docstring
            order=self.table._model.columns[self.table._state.sort_column],
            reverse=self.table._state.sort_backwards)
        aqt.gui_hooks.browser_will_search(context)
        if isinstance(context.order, Column) and not context.order.sortable:
            # What the backend does with a column registered SORTING_NONE — i.e. what the
            # user saw on every search when this hook overwrote Advanced Browser's SQL.
            self.failures.append(context.order.key)
            raise RuntimeError("Can't sort Cards by Custom.")
        if rows is not None:
            self.table._model.rows = list(rows)
        self.searches.append({"search": context.search, "order": str(context.order),
                              "reverse": bool(context.reverse)})
        aqt.gui_hooks.browser_did_search(context)
        # ...and the last thing `Table.search` does is restore the selection *Anki*
        # remembers, which is what any landing has to survive.
        self.table.current = remembers if remembers is not None else (
            self.table._model.rows[0] if self.table._model.rows else None)


temp_tables = []


def advanced_browser_will_search(context):
    """Advanced Browser's `willSearch`, in the shape that matters here.

    Its note-field columns cannot be sorted by the backend at all, so it swaps the column
    out for raw SQL — reading the *state* for the direction, which is why an order those
    views need has to go through the sort state and not through `context.order`. And
    before it can sort by one it **rebuilds a temp table of every note in the collection**
    (462,487 of them on the collection this was measured on: ~2.1s of the 2.25s a search
    took). `temp_tables` records each rebuild, so the test can hold it to zero for the
    views that carry their own SQL."""
    order = context.order
    if isinstance(order, Column) and order.key.startswith("_field_"):
        temp_tables.append(order.key)          # cc.sortTableFunction()
        sql = f"SQL({order.key})"
        if context.browser.table._state.sort_backwards:
            sql += " desc"
        context.order = sql


def _run_sql_for_real(order_sql):
    """Run the generated ORDER BY against real SQLite, on real note rows.

    The stub above can only prove the expression was *handed over*; this proves it runs.
    Bad SQL fails at prepare time inside Anki, i.e. as a modal on every search — the exact
    shape of the last bug this code shipped — and neither a Python stub nor a review reads
    SQL for syntax as well as SQLite does. `field_at_index` is Anki's own function over the
    0x1f-separated field blob, so it is registered here the same way.

    The rows are ordered so that a correct expression can only produce one answer: two
    notetypes with the field at different ordinals, a numeric-looking value, and an empty
    field that has to sort last whichever direction it is."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.create_function("field_at_index", 2,
                       lambda flds, index: (flds.split("\x1f")[index]
                                            if 0 <= index < len(flds.split("\x1f")) else None))
    db.execute("create table notes (id integer primary key, mid integer, flds text)")
    mvj, other = 1690000000000, 1700000000000
    db.executemany("insert into notes values (?,?,?)", [
        # MvJ: the field is ordinal 6, so seven separators before it.
        (1, mvj, "\x1f".join(["s", "w", "m", "3", "1", "0.5", "000010 あ 070 0012", "r"])),
        (2, mvj, "\x1f".join(["s", "w", "m", "3", "1", "0.5", "000200 い 060 0008", "r"])),
        (3, mvj, "\x1f".join(["s", "w", "m", "3", "1", "0.5", "", "r"])),
        # The other notetype carries it at ordinal 1.
        (4, other, "\x1f".join(["front", "000100 う 050 0004"])),
    ])
    try:
        rows = [r[0] for r in db.execute(f"select n.id from notes n order by {order_sql}")]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "order": rows}


mvj_config = {}          # what MvJ's own config (Settings ▸ Hotkeys) says right now


def _resolved_hotkeys(keys, hotkeys, col=None):
    """The browse keys, resolved. One source — this add-on's own config — whatever MvJ's
    Hotkeys tab says, because a row there is what makes MvJ bind its own rival."""
    global mvj_config
    was = mvj_config
    try:
        mvj_config = {"hotkeys": dict(hotkeys)}
        return keys._hotkeys()
    finally:
        mvj_config = was


def main():
    col = Col()
    aqt.mw = types.SimpleNamespace(
        col=col,
        addonManager=types.SimpleNamespace(getConfig=lambda name: mvj_config
                                           if name == "MvJ Japanese" else {}),
        pm=types.SimpleNamespace(profileFolder=lambda: "/nonexistent"),
        form=types.SimpleNamespace(menuTools=types.SimpleNamespace(addAction=lambda a: None)))

    # Registered *before* the add-on's, as Anki loads it: add-ons come up in folder-name
    # order and `874215009` sorts before `gigaku`.
    aqt.gui_hooks.browser_will_search.append(advanced_browser_will_search)

    from gigaku import queries
    from gigaku.core import browser as b
    from gigaku.core import conf
    from gigaku.features import browser_view, nav

    if len(aqt.gui_hooks.browser_will_search) < 2:  # the package installs itself on import
        browser_view.install()
    assert aqt.gui_hooks.browser_will_search[0] is not advanced_browser_will_search, \
        "the add-on must register ahead of Advanced Browser"


    counter = conf.section("counter")
    home = queries.home_query(conf.mvj_browser_query(), conf.am_tags()["ready"])
    alternates = queries.alt_query(counter["notetype"], counter["study_field"], "食べる")
    episode = '"Sentence Audio:*SHOW_01*"'
    out = {"hooks": [len(aqt.gui_hooks.browser_will_search),
                     len(aqt.gui_hooks.browser_did_search)],
           "view_sort": {kind: list(conf.view_sort(kind))
                         for kind in ("queue", "alternates", "context")}}

    browser = Browser(col)

    def step(name, run):
        before, writes, tables = len(browser.searches), len(col.writes), len(temp_tables)
        run()
        run_timers()
        last = browser.searches[-1]
        out[name] = {"searches": len(browser.searches) - before,
                     "temp_tables": len(temp_tables) - tables,
                     "order": last["order"], "reverse": last["reverse"],
                     "state_sort": browser.table._state.sort_column,
                     "config_writes": col.writes[writes:],
                     "indicator": browser.header.indicator_shown,
                     "current": browser.table.current,
                     "focused": browser.focused,
                     "remembered": [conf.view_state()["sort"],
                                    bool(conf.view_state()["descending"])]}

    def press(query, land=None, kind="queue", **kw):
        """A gigaku press: the order goes into the state first (`nav._order_first`), then
        one search, then the landing it asked for."""
        def run():
            nav._order_first(browser, kind)
            if land is not None:
                b.land_after(browser, land)
            browser.search_for(query, **kw)

        return run

    # J: open the queue.
    step("queue", press(home, land=b.select_top, rows=[1, 2, 3]))
    # O: MvJ's own search into the episode — not one of ours, and not prepared by anybody.
    step("context", lambda: browser.search_for(episode, rows=[7, 8, 9]))
    # A background op's refresh: the same query, run by Anki, with nothing pending.
    step("context_refresh", lambda: browser.search(rows=[7, 8, 9]))
    # O again: back to the queue, landing on the card it was pressed on. Anki's own restore
    # would put the selection on row 0, so the landing has to survive it.
    step("back", press(home, land=b.select_anchor([2, 3]), rows=[1, 2, 3]))
    out["back"]["intent_left"] = b.take_landing(browser) is not None
    # L: into the alternates, keeping the card Anki holds (the one L was pressed on).
    step("alternates", press(alternates, land=b.keep_selection, kind="alternates",
                             rows=[4, 5], remembers=5))

    def arrive_unprepared():
        """The sort state drifted — MvJ sorts by cardDue from several of its own actions —
        and then the queue is opened by something that didn't set the order first: a saved
        search in the sidebar, the browser's own startup search."""
        browser.table._state.sort_column = "cardDue"
        browser.table._state.sort_backwards = False
        browser._lastSearchTxt = ""          # force an arrival
        browser.search_for(home, rows=[1, 2, 3])

    step("unprepared", arrive_unprepared)

    def click_sort_in_alternates():
        browser.header.sectionClicked.emit(0)
        browser.table._state.sort_column = "cardDue"     # what Anki's click handler does
        browser.table._state.sort_backwards = False
        browser.search(rows=[4, 5])

    browser.search_for(alternates, rows=[4, 5])           # back into the alternates
    run_timers()
    step("alternates_click", click_sort_in_alternates)

    step("context_again", lambda: browser.search_for(episode, rows=[7, 8, 9]))

    def click_sort_in_context():
        browser.header.sectionClicked.emit(0)
        browser.table._state.sort_column = "noteFld"
        browser.search(rows=[7, 8, 9])

    step("context_click", click_sort_in_context)

    out["total_searches"] = len(browser.searches)
    out["failed_searches"] = list(browser.failures)
    out["temp_tables_total"] = len(temp_tables)
    out["preflights"] = len(col.preflights)
    out["sql_run"] = _run_sql_for_real(out["queue"]["order"])

    from gigaku.features import keys
    # Every row of MvJ's tab that used to be read, all naming keys this side holds:
    # none of them may move a key now.
    out["hotkeys"] = _resolved_hotkeys(keys, {"auto_tag_known": "Z", "browse_search_hotkey": "J",
                                              "browse_alternates": "L", "view_context": "I"}, col)
    out["tooltips"] = list(tooltips)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
