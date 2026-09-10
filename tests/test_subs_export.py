"""What a JavaScript dialog costs a rip, and what keeps one from ever appearing.

A dialog does not interrupt a `gigaku subs` run, it ends it: while one is up Chrome answers no
`execute javascript` on that tab, so every wait, every probe and every recovery path is left
waiting two minutes per call on a browser that has stopped talking. It happened for real — LR
raised "Subtitles not loaded, no data to export." from the Export button and the run sat there
with a finished episode on screen, printing nothing, with nothing left to notice it with.

Three things stop it happening again and are pinned here: LR's own export precondition is
waited for instead of clicked into, its alerts are captured in the main world rather than shown,
and a dialog that lands anyway is recovered from by replacing the tab (measured: navigation
does not clear one).

Importing lib.subs.subs costs pyobjc, unlike the pure-logic modules; the browser is
monkeypatched away.
"""
import itertools

import pytest

from lib.platform import chrome
from lib.platform.applescript import AppleScriptError
from lib.subs import subs


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """No tab checks, and a clock that moves so the adaptive waits can reach a give-up."""
    monkeypatch.setattr(subs, "_check_tab", lambda: None)
    monkeypatch.setattr(subs.time, "sleep", lambda _s: None)
    clock = itertools.count(0, 30)
    monkeypatch.setattr(subs.time, "time", lambda: next(clock))


def _browser(monkeypatch, *, state, dialog="", clicks=None):
    """Fake the two JS worlds: ``_run_main`` answers LR's state and the captured dialog,
    ``_njs`` answers the export modal. Returns the list of isolated-world calls made."""
    box = {"msg": dialog}
    calls: list[str] = []

    def run_main(body):
        if "__gigaku_dialog" in body:
            msg = box["msg"]
            if "window.__gigaku_dialog='';" in body:   # _dialog(clear=True) takes it
                box["msg"] = ""
            return {"msg": msg}
        d = dict(state() if callable(state) else state)
        # What the injected _EXPORTABLE computes in the page, from the same three fields.
        d["ok"] = d.get("state") == "SUBS_LOADED" and bool(d.get("lang")) and bool(d.get("nlp"))
        return d

    def njs(js):
        calls.append(js)
        if "MigakuShadowDom" in js:
            return "ok"
        if "lln-open-export-modal" in js:
            return "ready"
        if "b[i].click()" in js:
            box["msg"] = clicks or ""      # the click's own consequence
            return "ok"
        return "ok"

    monkeypatch.setattr(subs, "_run_main", run_main)
    monkeypatch.setattr(subs, "_njs", njs)
    return calls


def test_every_main_world_call_reinstalls_the_dialog_capture(monkeypatch):
    """A reload wipes the overrides, so they ride along with every call — like the visibility
    spoof and the playback block. Installing them once, at the start of a run, would leave
    every reloaded page unprotected."""
    sent = []
    monkeypatch.setattr(subs, "_njs", lambda js: sent.append(js) or "{}")
    subs._run_main("o.x=1;")
    assert "window.alert=function" in sent[0]
    assert "__gigaku_dialog" in sent[0]


def test_the_export_is_not_clicked_until_lr_says_it_can_be(monkeypatch):
    """The bug, exactly: subtitles loaded and translated (so ``_verify_tracks`` passed) but
    LR's other two conditions unmet, and clicking Export there raises the alert that wedges
    the tab. The wait gives up into the episode retry loop instead."""
    calls = _browser(monkeypatch, state={"state": "SUBS_LOADED", "lang": True, "nlp": False})
    with pytest.raises(subs.ExportError) as caught:
        subs._export()
    assert "LR ready to export" in str(caught.value)
    assert not any("b[i].click()" in c for c in calls), "clicked Export into the alert"


def test_a_refusal_is_reported_in_language_reactors_own_words(monkeypatch):
    """Captured, not suppressed: the run's log says why, instead of showing a step that
    silently did nothing."""
    _browser(monkeypatch,
             state={"state": "SUBS_LOADED", "lang": True, "nlp": True},
             clicks="Subtitles not loaded, no data to export.")
    with pytest.raises(subs.ExportError) as caught:
        subs._export()
    assert "no data to export" in str(caught.value)


def test_a_stale_refusal_is_never_read_as_this_clicks(monkeypatch):
    """An alert from an earlier attempt would otherwise fail an export that then downloads
    fine. The slate is cleared before the click."""
    monkeypatch.setattr(subs, "_xlsx_names", lambda: {"lln_excel_subs.xlsx"})
    _browser(monkeypatch,
             state={"state": "SUBS_LOADED", "lang": True, "nlp": True},
             dialog="Export error: something from last time")
    # No new file ever appears, so this still fails — but on the download, not on the stale
    # sentence, and the message is the wait's rather than LR's.
    with pytest.raises(subs.ExportError) as caught:
        subs._export()
    assert "download" in str(caught.value)


def test_a_dialog_that_lands_anyway_replaces_the_tab_and_retries(monkeypatch):
    """``_NO_DIALOGS`` only covers the page from the moment it is installed; a reload wipes it.
    So the timeout has to be recoverable — and by *closing* the tab, since a reload does not
    clear a dialog (measured: accepted, then `loading` for ever with JS still blocked)."""
    def blocked(_match, _js):
        raise AppleScriptError("AppleEvent timed out.", error_number=-1712)

    scripts = []
    monkeypatch.setattr(subs, "exec_js_on_extension", blocked)
    monkeypatch.setattr(subs, "_tab_state", lambda: ("watch", "https://www.netflix.com/watch/42"))
    monkeypatch.setattr(subs, "_WATCH_URL", "https://www.netflix.com/watch/42")
    monkeypatch.setattr(subs, "_as", lambda src: scripts.append(src) or "ok")

    with pytest.raises(subs.ExportError) as caught:
        subs._njs("1+1")
    assert "dialog" in str(caught.value)
    assert scripts and "close tab" in scripts[0] and "make new tab" in scripts[0]
    assert "watch/42" in scripts[0]


def test_a_timeout_with_no_tab_left_is_not_dressed_up_as_a_dialog(monkeypatch):
    """The detector is "JS times out while plain AppleScript still answers about the tab".
    With no tab there is nothing to reopen, and the timeout is reported as what it was."""
    def blocked(_match, _js):
        raise AppleScriptError("AppleEvent timed out.", error_number=-1712)

    monkeypatch.setattr(subs, "exec_js_on_extension", blocked)
    monkeypatch.setattr(subs, "_tab_state", lambda: ("gone", ""))
    monkeypatch.setattr(subs, "_as", lambda src: pytest.fail("reopened a tab that is gone"))

    with pytest.raises(AppleScriptError):
        subs._njs("1+1")


def test_javascript_calls_are_bounded(monkeypatch):
    """Unbounded, one dialog reduces a run to six fruitless minutes per JS call: measured, a
    blocked `execute javascript` took 120.2s to fail with -1712 and ``applescript.run`` retries
    that three times as a transient hiccup. `with timeout` turned the same block into 8.2s."""
    sent = []
    monkeypatch.setattr(chrome.applescript, "run", lambda src: sent.append(src) or "ok")
    chrome.exec_js_on_extension("netflix.com/watch", "1+1")
    assert f"with timeout of {chrome.JS_TIMEOUT} seconds" in sent[0]
    assert "end timeout" in sent[0]
