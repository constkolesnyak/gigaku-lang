"""The add-on's browser navigation, pinned: one search per press, and whose sort is whose.

This is the first coverage the Anki side's *GUI* half has had, and it exists twice over.
The machinery it replaced was untestable by construction — a ladder of timers re-asserting
a view until it agreed with itself — and what replaced it then shipped a bug that broke
every search in the browser, because `browser_will_search` is a hook **Advanced Browser is
also on**: for its `_field_<name>` columns it swaps `context.order` for raw SQL (the
backend cannot sort by a note field, and it registers those columns as unsortable), so
setting such a column on the search afterwards threw that SQL away and every search died
with "Can't sort Cards by Custom". The harness therefore simulates Advanced Browser, hook
order included, and raises where the backend raises.

The promises, in order of what they cost when broken:

1. **Nothing this add-on does may break a search.** An order it does not own goes through
   the sort state — what every owner's hook resolves from — and never onto the search.
2. **The episode view's order never becomes anybody else's.** MvJ's `O` shows an episode,
   which reads in the order it was watched, carried on the search alone — so the sort the
   user chose for the queue and the alternates is still there when they come back. Anki's
   sort state persists to the collection config, so "never written" is the only version of
   that promise that holds.
3. **One search per press.** The old shape sorted the table and then searched again to make
   it take; on a 460k-card collection that second search is what the press felt like.
4. **A landing survives Anki's own selection restore**, which is the last thing a search
   does and the thing that used to eat it.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "addon_browser_harness.py"

REMEMBERED = "_field_My Sort Key"
EPISODE = "Column:noteCrt"


@pytest.fixture(scope="module")
def run():
    proc = subprocess.run([sys.executable, str(HARNESS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_no_search_was_broken(run):
    """The regression that shipped: every search dying on an unsortable column."""
    assert run["failed_searches"] == []


def test_the_hook_is_shared_with_advanced_browser(run):
    # Two on will_search — Advanced Browser's and ours, with ours first (the harness
    # asserts the position; `test_no_temp_table_is_ever_built` is what it buys).
    assert run["hooks"] == [2, 1]


def test_only_the_episode_view_has_an_order_of_its_own(run):
    assert run["view_sort"]["queue"] == run["view_sort"]["alternates"] == [REMEMBERED, True]
    # Ascending: descending would play the episode backwards.
    assert run["view_sort"]["context"] == ["noteCrt", False]


@pytest.mark.parametrize("step", ["queue", "context", "back", "alternates", "context_again"])
def test_one_search_per_press(run, step):
    assert run[step]["searches"] == 1


@pytest.mark.parametrize("step", ["queue", "back", "alternates", "unprepared"])
def test_a_note_field_sort_is_computed_not_tabulated(run, step):
    """The remembered order is a note field, which the backend cannot sort by. Advanced
    Browser answers that with a temp table rebuilt from every note in the collection on
    every search — measured at ~2.1s of a 2.25s press. The field is read out of the note
    row instead, so the sort costs a field extraction per *result* row."""
    order = run[step]["order"]
    assert "field_at_index" in order, order
    assert "collate nocase desc nulls last" in order, order


def test_no_temp_table_is_ever_built(run):
    """Which only holds because this hook is registered *ahead* of Advanced Browser's: it
    builds the table the moment it sees one of its columns, before rewriting the order, so
    replacing the order afterwards skips the rewrite and pays for the table anyway."""
    assert run["temp_tables_total"] == 0


def test_the_sort_sql_runs_and_orders_correctly(run):
    """Executed against real SQLite, on note rows shaped like Anki's. Bad SQL here is a
    modal on every search, and no amount of reading catches what SQLite catches."""
    assert run["sql_run"]["ok"], run["sql_run"].get("error")
    # Descending by the sort key: 000200, then 000100 (a second notetype, where the field
    # sits at a different ordinal), then 000010 — and the empty field last, not first.
    assert run["sql_run"]["order"] == [2, 4, 1, 3]


def test_the_sql_is_proven_once_before_it_is_trusted(run):
    """One preflight for the whole session: the expression is run against a search that can
    match at most one card, and a failure falls back to the slow path instead of a modal."""
    assert run["preflights"] == 1


def test_arriving_applies_the_remembered_order_to_the_state_too(run):
    # ...so the searches Anki runs on its own keep it.
    assert run["queue"]["state_sort"] == REMEMBERED


def test_the_episode_view_orders_the_search_and_writes_nothing(run):
    assert run["context"]["order"] == EPISODE
    assert run["context"]["reverse"] is False
    assert run["context"]["state_sort"] == REMEMBERED, "the sort state must not move"
    assert not [key for key in run["context"]["config_writes"]
                if key.startswith("sort")], "the sort must not reach the collection config"
    # The header's arrow still names the remembered column, and the rows are not in it.
    assert run["context"]["indicator"] is False


def test_the_episode_order_survives_a_refresh_it_did_not_ask_for(run):
    # A background op re-runs the same search: not an arrival, and still the episode's
    # order — otherwise the view silently falls back to the remembered one.
    assert run["context_refresh"]["order"] == EPISODE


def test_an_arrival_nobody_prepared_costs_no_extra_search(run):
    """A saved search opened with the sort state drifted (MvJ sorts by cardDue from several
    of its own actions). The corrected order lands on that same search, because a field
    sort is now ours to compute; the re-run is the fallback for a field we cannot prove is
    plain text, where only Advanced Browser can produce a sortable order."""
    assert run["unprepared"]["searches"] == 1


def test_the_way_back_lands_on_the_anchor_and_takes_the_focus(run):
    assert run["back"]["current"] == 2, "Anki's own restore (row 0) must not win"
    assert run["back"]["focused"] == "table"
    assert run["back"]["intent_left"] is False, "a landing is consumed once"


def test_entering_the_alternates_keeps_the_card_and_takes_the_focus(run):
    assert run["alternates"]["current"] == 5, "the card L was pressed on stays selected"
    assert run["alternates"]["focused"] == "table"


def test_a_sort_the_user_clicks_is_remembered(run):
    assert run["alternates_click"]["remembered"] == ["cardDue", False]


def test_a_sort_clicked_inside_the_episode_view_is_obeyed_but_not_remembered(run):
    assert run["context_click"]["order"] == "Column:noteFld", "the click must do something"
    assert run["context_click"]["remembered"] == ["cardDue", False], "...and nothing more"


def test_the_keys_have_one_source_and_mvjs_dialog_is_not_it(run):
    """Reading MvJ's Hotkeys rows for these keys, and writing this add-on's defaults into
    them, were both tried in one evening and both taken back out. A filled row there is
    what makes *MvJ* bind its own version of the action, so every row naming a key this
    side already held put two claims on it — and Qt answers two claims by firing neither.
    That was "j does nothing", "View Context doesn't work" and "the shortcuts are slow and
    glitchy", in that order. This pins the keys against a dialog whose every relevant row
    is filled with one of them."""
    keys = run["hotkeys"]
    assert keys["row_down"] == "J" and keys["row_up"] == "K"
    assert keys["flag"] == "N" and keys["trim"] == "U" and keys["ask"] == "Y"
    # `M`, not `I`: `I` belongs to MvJ's View Context, and one key means one binding.
    assert keys["known"] == "M"


def test_the_arrival_says_which_view_it_is(run):
    assert run["tooltips"][0].startswith("my-learn ·")
    assert any(t.startswith("alternates") for t in run["tooltips"])
