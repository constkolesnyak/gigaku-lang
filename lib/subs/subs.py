#!/usr/bin/env python3
"""Autonomously rip a whole Netflix season's subtitles via Language Reactor.

For every not-yet-done episode of the season the current tab is on — or just the ones an
``N-M`` range asks for — this selects the requested audio + ASR subtitle track, waits for
Language Reactor's machine translations, drives LR's Export dialog to download one Excel
file, then converts that into two aligned SRT files — ``<Title> - S01E03 - Primary.srt``
(target language) and ``… - Secondary.srt`` (native translation) — inside
``~/Downloads/<Title>/``.

Everything is driven through Language Reactor's own in-page controller (``window.lln``)
and Netflix's player metadata, executed via AppleScript ``execute javascript`` — no
cursor/screen-coordinate automation.

Two execution worlds matter (see the module docstring of lib/platform/chrome.py): ``execute
javascript`` runs in an *isolated* world that shares the DOM but not page globals, so
``window.lln`` / ``netflix`` are only reachable by injecting a ``<script>`` into the
page's main world and reading its result back through a shared DOM node. ``_run_main()``
does that; ``_njs()`` is plain DOM.

Preconditions:
  * A Netflix tab is open in Chrome, on any episode of the target season (``/watch/…``) or
    just on the show itself (``/title/…``, where clicking a show lands you) — from a title
    page the run starts the player itself. The script targets the tab by URL and does NOT
    need it focused: it spoofs page-visibility so Netflix keeps ripping while the tab is
    backgrounded, so just launch and keep using the computer. To stop: Ctrl-C in the
    terminal, or close the tab; re-run to resume.
  * Chrome ▸ View ▸ Developer ▸ "Allow JavaScript from Apple Events" is enabled.
  * The Language Reactor extension is installed, **signed in**, with Pro active (ASR
    subtitles + machine translation are Pro; signed out, LR cannot see the subscription and
    the run refuses with that as the reason rather than telling you to subscribe). Its on/off toggle need not be on: if LR is toggled off the run
    turns it on itself, and turns it back off when it finishes.

Usage:
  gigaku subs [N-M] [AUDIO] [SUBTITLE]  # defaults: whole season, German / "ASR Pro German"
  gigaku subs 3-12                      # only episodes 3–12 (3- to the end, -3 from it)
  uv run python lib/subs/subs.py "German" "ASR Pro German" 3-12
"""

import json
import queue
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.platform import applescript  # noqa: E402
from lib.platform.applescript import AppleScriptError  # noqa: E402
from lib.platform.chrome import SPOOF_VIS as _SPOOF_VIS  # noqa: E402
from lib.platform.chrome import exec_js_on_extension, open_tab  # noqa: E402
from lib.platform.power import DisplayWakeLock  # noqa: E402
from lib.config import (  # noqa: E402
    CHROME_DOWNLOAD_DIR,
    NETFLIX_MATCH,
    NETFLIX_WATCH_MATCH,
    NF_STALL_GIVEUP,
    UserError,
    note,
    noting,
    settings,
)
from lib.subs import episode_range, excel_to_srt, library, naming  # noqa: E402

_JS_OFF = (
    "Chrome is blocking JavaScript from Apple Events. Enable it once: "
    "Chrome ▸ View ▸ Developer ▸ 'Allow JavaScript from Apple Events'."
)

# Serialize every AppleScript call: NSAppleScript isn't safe under concurrent calls, and a
# background pause-keeper thread (below) drives the browser at the same time as the main flow.
_JS_LOCK = threading.RLock()

# When set, the pause-keeper thread keeps the video paused (~1×/s). Set for the WHOLE run and
# cleared only when it ends: the video never plays, not even to generate ASR subtitles.
_PAUSE_KEEP = threading.Event()


class ExportError(Exception):
    """A step in the flow failed (recoverable — triggers a reload/retry)."""


class _Aborted(Exception):
    """Stop the run — with the reason that was actually observed.

    It carries ``reason`` because the ending used to *guess* it: every abort path printed
    "Stopped (Ctrl-C or tab closed)", including the paths where neither had happened. A run
    can be an hour deep, so the one line it ends on has to be true.
    """

    def __init__(self, reason: str = "the Netflix tab was closed"):
        super().__init__(reason)
        self.reason = reason


# ── JavaScript execution ─────────────────────────────────────────────────────

def _as(source: str) -> str | None:
    """Run raw AppleScript, serialized against all other browser calls."""
    with _JS_LOCK:
        return applescript.run(source)


def _njs(js: str) -> str | None:
    """Run JS on the Netflix tab's isolated world (DOM access only)."""
    try:
        with _JS_LOCK:
            return exec_js_on_extension(NETFLIX_WATCH_MATCH, js)
    except AppleScriptError as e:
        if "turned off" in str(e):
            raise ExportError(_JS_OFF) from e
        if e.error_number == -1712:      # the renderer answered nothing — see below
            _clear_dialog_wedge()
        raise


def _clear_dialog_wedge() -> None:
    """A JavaScript dialog is up on the tab: replace the tab, so the run can go on.

    ``_NO_DIALOGS`` stops LR from raising one, but only from the moment it is installed — a
    reload wipes it, and a dialog raised in the gap before the next main-world call (or by
    anything that is not LR's page script) still lands. It has to be recoverable, because it
    is the one failure that disables the machinery that would otherwise notice it: while a
    dialog is up Chrome answers no JS on that tab at all.

    Two measured facts decide the shape. Plain AppleScript keeps answering about the same tab
    while JS times out, so the combination *is* the detector — a busy Chrome fails both, a
    modal dialog fails exactly one. And navigation does **not** clear a dialog: ``reload`` and
    ``set URL`` are accepted and the tab then reports ``loading`` for ever with JS still
    blocked, while closing the tab clears it at once. So the tab is replaced, not reloaded, and
    the caller gets the recoverable error its retry loop already answers with a reload.
    """
    state, url = _tab_state()      # non-JS: still answers while the renderer is blocked
    if state == "gone":
        return                     # nothing to clear — let the timeout be reported as it is
    target = _WATCH_URL or url
    print(f"  a JavaScript dialog is blocking the tab — reopening {target}")
    _as('tell application "Google Chrome"\n'
        "  repeat with w in windows\n"
        "    repeat with i from (count of tabs of w) to 1 by -1\n"
        f'      if URL of tab i of w contains "{NETFLIX_MATCH}" then\n'
        f'        make new tab at end of tabs of w with properties {{URL:"{target}"}}\n'
        "        close tab i of w\n"
        '        return "ok"\n'
        "      end if\n"
        "    end repeat\n"
        "  end repeat\n"
        '  return "none"\n'
        "end tell")
    raise ExportError("a JavaScript dialog blocked the tab — reopened it, retrying")


# _SPOOF_VIS — "make the page permanently believe it is foreground" — now lives in
# lib/platform/chrome.py as SPOOF_VIS and is imported above, because `gigaku titles` needs the
# same trick to rip a browse gallery from a background tab. It is prepended to every
# _run_main body so it is (re)asserted on every main-world call, including right after a page
# reload (which wipes it). Netflix tears the player down / won't switch or generate ASR
# subtitles in a hidden tab, and this is what lets the whole rip run while the tab sits in the
# background — so the user can switch tabs/apps and keep working.


# Make playback IMPOSSIBLE, not merely undone. The pause-keeper thread only *reacts* — it
# pauses ~1×/s — while Netflix restarts the player on its own after every navigation, so each
# cycle leaked up to a second of real, audible playback and the position crept forward (measured
# drifting 0 → 18s while every sampled `paused` read true, because the samples kept landing just
# after a pause). Neutralising play() at the source stops it happening at all: the method becomes
# a no-op returning a resolved promise (so anything awaiting it still resolves), and a capturing
# 'play'/'playing' listener pauses anything that starts by another route. Re-asserted on every
# call like _SPOOF_VIS, since a reload wipes it. Idempotent; never touches ``o``.
_NO_PLAY = (
    "try{if(!window.__gigaku_noplay){window.__gigaku_noplay=1;"
    "HTMLMediaElement.prototype.play=function(){try{this.pause();}catch(_e){}"
    "return Promise.resolve();};"
    "document.addEventListener('play',function(ev){try{ev.target.pause();}catch(_e){}},true);"
    "document.addEventListener('playing',function(ev){try{ev.target.pause();}catch(_e){}},true);}"
    "var _v=document.querySelector('video');if(_v&&!_v.paused){_v.pause();}"
    "}catch(_e){}"
)


# Take LR's dialogs away from the browser and hand them to the run. A JS dialog does not
# interrupt a rip, it ENDS it — while one is up Chrome answers no `execute javascript` on that
# tab, so every wait and every recovery path is left waiting 120s per call on a browser that
# has stopped talking (see ``JS_TIMEOUT`` in lib/platform/chrome.py, and ``_clear_dialog_wedge``
# for what it then costs to get out). Measured on the wedge that prompted this: LR raised
# "Subtitles not loaded, no data to export." from the Export button, and the run sat there with
# a finished episode on screen, printing nothing — reloading does not clear a dialog either
# (``reload`` and ``set URL`` are both accepted, the tab then reports `loading` for ever and JS
# stays blocked); only closing the tab does. So the dialogs are neutralised at the source rather
# than recovered from.
#
# It reaches LR because LR's alerts are in ``pageScript_lln.min.js``, which the extension
# declares a web-accessible resource and injects into the page's MAIN world — the same world
# ``window.lln`` lives in, and the one this prelude runs in. (A content script's ``alert`` would
# be out of reach: every extension gets its own isolated world, including the one AppleScript
# executes in.) The message is kept, never swallowed: ``_dialog()`` reads it back, so a refusal
# is reported in LR's own words instead of as a silent skip. Idempotent, wiped by a reload and
# re-asserted on the next call, like ``_SPOOF_VIS``.
_NO_DIALOGS = (
    "try{if(!window.__gigaku_nodialog){window.__gigaku_nodialog=1;window.__gigaku_dialog='';"
    "window.alert=function(m){window.__gigaku_dialog=''+m;};"
    "window.confirm=function(m){window.__gigaku_dialog=''+m;return false;};"
    "window.prompt=function(m){window.__gigaku_dialog=''+m;return null;};}"
    "}catch(_e){}"
)


def _run_main(body: str) -> dict:
    """Run ``body`` in the page's MAIN world and return its ``o`` object as a dict.

    ``body`` must assign results onto the pre-declared ``o`` object. Page globals
    (``window.lln``, ``netflix``) are reachable here, unlike in ``_njs``. Never raises on
    in-page errors: a thrown body yields ``{"__err": ...}`` and a mid-navigation page
    (globals not ready) yields ``{}`` — callers poll. Every call re-asserts the visibility
    spoof (``_SPOOF_VIS``) so a backgrounded tab keeps ripping, the playback block
    (``_NO_PLAY``) so the video can never start, and the dialog capture (``_NO_DIALOGS``) so an
    LR alert can never wedge the tab.
    """
    payload = (
        "(function(){var el=document.getElementById('__gigaku_x__');var o={};"
        "try{" + _SPOOF_VIS + _NO_PLAY + _NO_DIALOGS + body
        + "}catch(e){o.__err=''+((e&&e.message)||e);}"
        "el.textContent=JSON.stringify(o);})();"
    )
    wrapper = (
        "var d=document.getElementById('__gigaku_x__');"
        "if(!d){d=document.createElement('div');d.id='__gigaku_x__';"
        "d.style.display='none';document.documentElement.appendChild(d);}"
        "d.textContent='';"
        "var s=document.createElement('script');s.textContent=" + json.dumps(payload) + ";"
        "document.documentElement.appendChild(s);s.remove();d.textContent"
    )
    raw = _njs(wrapper)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"__raw": raw}


def _dialog(clear: bool = False) -> str:
    """The last dialog message ``_NO_DIALOGS`` caught, or "" — optionally taking it.

    This is the whole point of capturing rather than suppressing: LR says *why* it refused
    ("Subtitles not loaded, no data to export."), and that sentence is worth more in the run's
    log than a step that silently did nothing.
    """
    d = _run_main("o.msg=window.__gigaku_dialog||'';"
                  + ("window.__gigaku_dialog='';" if clear else ""))
    return d.get("msg") or ""


def _wait_progress(measure, done, progress, desc: str, *, on_measure=None,
                   on_stall=None, log=None, interval: float = 2.0,
                   giveup: float | None = None) -> dict:
    """Adaptive wait — no fixed deadline.

    Blocks as long as ``progress(result)`` keeps changing, so it rides out a slow server
    and returns the instant ``done(result)`` is true. ``on_stall(result)`` runs on ticks with
    no progress. Raises only after ``giveup`` (or ``NF_STALL_GIVEUP``) seconds with no change
    in ``progress`` — a real stall.

    ``on_measure(result)`` runs after **every** measurement and before ``done``, which is what
    a check on something the wait isn't measuring progress by needs: a track that reverted
    mid-wait (``_TrackWatch``) has to be caught while the counts are still climbing happily,
    so ``on_stall`` — which by definition only fires when nothing is moving — can't see it.

    Every tick starts with ``_check_tab``, so no caller has to remember to pass it: a wait is
    exactly where a run sits while the browser goes somewhere unexpected.
    """
    last_key: object = _wait_progress  # unique sentinel
    last_change = time.time()
    last_log = 0.0
    d: dict = {}
    while True:
        _check_tab()
        try:
            d = measure()
        except AppleScriptError as e:
            if "turned off" in str(e):
                raise ExportError(_JS_OFF) from e
            d = {}  # tab momentarily gone mid-navigation
        if on_measure:
            try:
                on_measure(d)
            except AppleScriptError:
                pass  # a repair that couldn't reach Chrome this tick — try again next one
        if done(d):
            return d
        now = time.time()
        key = progress(d)
        if key != last_key:
            last_key, last_change = key, now
        else:
            # ``on_stall`` returning True means it deliberately waited (e.g. backing off a
            # server rate limit) — that's a handled pause, not a stall, so the clock restarts
            # instead of counting toward giving up.
            handled = False
            if on_stall:
                try:
                    handled = bool(on_stall(d))
                except AppleScriptError:
                    pass
            if handled:
                last_change = time.time()
            else:
                limit = NF_STALL_GIVEUP if giveup is None else giveup
                if now - last_change > limit:
                    raise ExportError(f"{desc}: no progress for {limit:g}s (last={d})")
        if log and now - last_log > 4:
            last_log = now
            log(d)
        time.sleep(interval)


# ── Tab / navigation / abort ─────────────────────────────────────────────────
#
# The whole flow targets the Netflix watch tab by URL, wherever it is — it does NOT steal
# focus or require the tab to be the active/foreground one. That's possible because
# ``_run_main`` spoofs page-visibility (``_SPOOF_VIS``), so Netflix keeps playing/switching/
# generating ASR while the tab sits in the background. The user launches ``gigaku subs`` and
# is free to switch tabs and apps. To stop: press Ctrl-C in the terminal, or close the tab.

def _chrome_tab(action: str, match: str = NETFLIX_WATCH_MATCH) -> str:
    """Run ``action`` (an AppleScript statement using ``t`` = the tab) on the first Chrome tab
    whose URL contains ``match``, wherever it lives. Returns "ok", or "none" if there is no
    such tab. ``match`` widens to any Netflix tab when a wandered-off tab has to be steered
    back — see ``_recover_tab``."""
    return _as(
        'tell application "Google Chrome"\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        f'      if URL of t contains "{match}" then\n'
        f"        {action}\n"
        '        return "ok"\n'
        "      end if\n"
        "    end repeat\n"
        "  end repeat\n"
        '  return "none"\n'
        "end tell"
    )


# Where the run's tab is *supposed* to be — the episode currently being ripped. Written by
# ``_goto`` and by ``run`` (for the episode the user launched from), read only by
# ``_recover_tab``, which needs somewhere to send a tab Netflix navigated away.
_WATCH_URL = ""

# A single reading of "no watch tab" is routine: Chrome is momentarily busy, or the page is
# swapping documents. A run can be hours old and cannot be resumed mid-episode, so the state
# has to hold across a few readings before it is allowed to end anything.
_TAB_TRIES = 4
_TAB_DELAY = 2.0

# When the run last put a URL into the tab. A tab that vanishes *seconds* after that is not a
# tab the user closed — measured 2026-08-14: a Netflix tab opened here loaded completely (its
# title read "Watch The Eminence in Shadow | Netflix" at 1.0s) and was **removed** from Chrome
# at 3.5s, with no navigation and no error page; `example.com` opened in the same window at the
# same moment was still there a minute later, and the same thing happened in an incognito
# window, where extensions are off. That is what a system-level restriction on netflix.com
# looks like from in here — macOS Screen Time's website limits remove the tab rather than
# showing a block page. Nothing in this file can hold such a tab open, so the one useful thing
# it can do is say so instead of reporting "the tab was closed" at a user who closed nothing.
_LAST_NAV = 0.0
TAB_KILL_WINDOW = 30.0

# How many times a run will put a vanished tab back before it gives up and says why. A cap,
# not a policy: reopening is cheap and an episode retry rebuilds the LR state anyway, but a tab
# that will not stay open is a condition no amount of reopening fixes, and hammering it forever
# would leave the run looking alive while achieving nothing.
REOPEN_LIMIT = 5
_REOPENS = 0


def _tab_state() -> tuple[str, str]:
    """What Chrome actually has right now: ``("watch", url)``, ``("away", url)`` (a Netflix
    tab that isn't on a watch page), ``("gone", "")`` — or ``("unknown", "")`` when Chrome
    said nothing at all.

    Asked as a question rather than inferred from an error message. The old check ran JS and
    read "…not found in Chrome" (AppleScript -2700) as *tab closed* — but that error means
    only "no tab's URL contains netflix.com/watch", which Netflix causes by itself whenever it
    bounces the tab to /browse, a title page or an error/login page. The tab is still open
    there, so calling it closed both lied and threw away a recoverable run.

    **"unknown" is the same lesson learned a second time.** ``applescript.run`` hands back
    ``stringValue()``, which is ``None`` when Chrome answers nothing, and this used to read
    ``(raw or "gone")`` — so *silence* became "there is no Netflix tab", which ends the season.
    It happened for real on The Eminence in Shadow E16 (2026-08-14): the run stopped with "the
    Netflix tab was closed" while that very tab sat there on `watch/81642113`, the episode it
    had just navigated to. Chrome not answering is not Chrome saying no.
    """
    # Separator is ``linefeed``, not ``tab`` — ``tab`` is a *class* in Chrome's AppleScript
    # dictionary, so inside this tell block it is the type, not the character.
    raw = _as(
        'tell application "Google Chrome"\n'
        '  set away to ""\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      set u to URL of t as text\n"
        f'      if u contains "{NETFLIX_WATCH_MATCH}" then return "watch" & linefeed & u\n'
        f'      if u contains "{NETFLIX_MATCH}" and away = "" then set away to u\n'
        "    end repeat\n"
        "  end repeat\n"
        '  if away = "" then return "gone" & linefeed\n'
        '  return "away" & linefeed & away\n'
        "end tell"
    )
    if not raw:
        return "unknown", ""
    state, _, url = raw.partition("\n")
    return state, url


def _chrome_tabs_total() -> int | None:
    """How many tabs Chrome has, of any kind — or None if it wouldn't say.

    The question that tells "no Netflix tab is open" apart from "Chrome isn't answering". A
    browser with thirty tabs and none of them Netflix has genuinely lost the run's tab; a
    browser that can't count its own tabs has told us nothing, and a season must not end on
    nothing.
    """
    raw = _as('tell application "Google Chrome"\n'
              "  set n to 0\n"
              "  repeat with w in windows\n"
              "    set n to n + (count of tabs of w)\n"
              "  end repeat\n"
              "  return (n as text)\n"
              "end tell")
    try:
        return int((raw or "").strip())
    except ValueError:
        return None


def _netflix_tabs() -> list[str]:
    """Every Netflix tab Chrome has open, in the order Chrome walks them.

    The same walk ``_tab_state`` does, kept whole instead of stopping at the first match —
    which is the one question none of the other helpers can answer, because each of them is
    built to stop there. Read by ``_require_one_netflix_tab``.
    """
    raw = _as(
        'tell application "Google Chrome"\n'
        '  set found to ""\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      set u to URL of t as text\n"
        f'      if u contains "{NETFLIX_MATCH}" then set found to found & u & linefeed\n'
        "    end repeat\n"
        "  end repeat\n"
        "  return found\n"
        "end tell"
    )
    return [u for u in (raw or "").splitlines() if u.strip()]


def _confirm_tab() -> tuple[str, str]:
    """``_tab_state`` again until it says "watch", or the tries run out. Every auto-stop in the
    run funnels through here, so one unlucky reading must not be what ends it."""
    state, url = _tab_state()
    for _ in range(_TAB_TRIES - 1):
        if state == "watch":
            break
        time.sleep(_TAB_DELAY)
        state, url = _tab_state()
    return state, url


def _recover_tab(url: str) -> None:
    """The tab is open but off the watch page — steer it back, or stop with the real reason.

    Netflix navigates its own tab away (a player error, an expired session, the end of a
    title), and the answer to that is to go back, not to end the season. Raises ``ExportError``
    once the watch page is back, so the load/episode retry loops rebuild the page state the
    same way they do after any other stumble.
    """
    if _WATCH_URL:
        print(f"  Netflix left the watch page (now {url}) — going back")
        try:
            _chrome_tab(f'set URL of t to "{_WATCH_URL}"', match=NETFLIX_MATCH)
        except AppleScriptError:
            pass
        for _ in range(10):
            time.sleep(2)
            if _tab_state()[0] == "watch":
                raise ExportError("Netflix left the watch page — went back, retrying")
    raise _Aborted(f"the Netflix tab left the watch page and wouldn't go back (it is at {url})")


def _reopen_tab() -> None:
    """The tab is gone seconds after this run put a URL in it — put it back and carry on.

    A tab that dies inside ``TAB_KILL_WINDOW`` of our own navigation never had a chance to be
    closed *by the user*: they had nothing to look at yet. Something else removed it (see the
    troubleshooting section in CLAUDE.md — measured 2026-08-14, a fully loaded Netflix tab
    removed 2.5s later, repeatedly, in a Chrome where Safari and a second Chrome instance were
    both unaffected). The run knows exactly which episode it was on, so the answer is to open
    that page again and let the episode's own retry rebuild the Language Reactor state — the
    same recovery every other stumble here gets.

    **The "close the tab to stop the run" gesture survives this**, because it lives outside the
    window: a tab you close while it sits there playing is one this never touches. Inside the
    window the run says loudly what it is doing, and Ctrl-C still ends it at once.

    Raises ``ExportError`` once the page is back (recoverable — the caller retries), or
    ``_Aborted`` when the tab will not stay open, which is the one case worth ending on.
    """
    global _REOPENS, _LAST_NAV
    _REOPENS += 1
    if _REOPENS > REOPEN_LIMIT:
        raise _Aborted(
            f"the Netflix tab was removed {_REOPENS - 1} times, each within seconds of being "
            "opened — that is not a tab you closed, it is something removing netflix.com tabs "
            "in Chrome. Measured elsewhere on this machine: Safari and a second Chrome "
            "instance keep such a tab fine, so look at what is scripting or restricting this "
            "Chrome (Screen Time website limits, a blocker, an automation). Nothing here can "
            "hold a tab that something else deletes")
    print(f"  the Netflix tab vanished seconds after we opened it — putting it back "
          f"({_REOPENS}/{REOPEN_LIMIT}): {_WATCH_URL}")
    try:
        open_tab(_WATCH_URL)
    except AppleScriptError as e:
        raise ExportError(f"couldn't reopen the Netflix tab: {e}") from e
    _LAST_NAV = time.time()
    for _ in range(15):
        time.sleep(2)
        if _tab_state()[0] == "watch":
            raise ExportError("the Netflix tab vanished — reopened it, retrying the episode")
    raise _Aborted(
        f"the Netflix tab would not stay open at {_WATCH_URL} — something in this Chrome is "
        "removing netflix.com tabs; Safari and a second Chrome instance were measured "
        "unaffected, so the cause is that browser's own restrictions or automation")


def _check_tab() -> None:
    """The run's sole auto-stop, and it only stops for something that has actually happened.

    A *hidden* tab is not a stop — visibility is spoofed, so a backgrounded tab keeps ripping
    and the user keeps working. A tab that merely wandered off the watch page is not a stop
    either: that is ``_recover_tab``'s job. Only a Netflix tab that is gone from Chrome ends
    the run, and then it says so.
    """
    state, url = _confirm_tab()
    if state == "watch":
        return
    if state == "unknown":
        # Chrome said nothing, repeatedly. That is not an answer, and a season may not end on
        # one — hand the caller its recoverable error instead, which reloads and asks again.
        raise ExportError("Chrome didn't answer about its tabs — retrying")
    if state == "gone":
        # Before ending a run that may be hours old, ask the one question that tells a closed
        # tab from a silent browser: how many tabs are there at all? A Chrome that can't count
        # its own tabs hasn't told us the Netflix one is missing.
        if not _chrome_tabs_total():
            raise ExportError("Chrome wouldn't say what tabs it has — retrying")
        if time.time() - _LAST_NAV < TAB_KILL_WINDOW and _WATCH_URL:
            _reopen_tab()          # raises: ExportError once it is back, _Aborted if it won't
        raise _Aborted("the Netflix tab was closed")
    _recover_tab(url)


# Navigating is asynchronous: `set URL`/`reload` return the moment Chrome accepts them, while
# the OLD document keeps answering JS for a second or two. That document is fully loaded, so a
# state probe right after navigating reports SUBS_LOADED with the right movie id and every wait
# returns *instantly* — against a page that is about to be thrown away. The next main-world call
# then lands on the new blank document and dies with "lln is not defined". So: stamp the current
# document, navigate, and block until an *unstamped* one answers.

def _navigate(action: str) -> None:
    """Run a navigating AppleScript statement and wait for the new document to be live."""
    global _LAST_NAV
    _LAST_NAV = time.time()
    # Read the stamp back: if it didn't take (page mid-navigation, JS momentarily unreachable)
    # then "unstamped" is true from the start and the wait below would return instantly —
    # silently restoring the very race it exists to prevent. Fall back to a settle pause.
    stamped = _run_main("window.__gigaku_page=1;o.k=1;o.stamped=!!window.__gigaku_page;").get("stamped")
    if _chrome_tab(action) != "ok":
        # No watch tab took the navigation. _check_tab decides what that means — closed, or
        # merely bounced off the watch page — and if it turns out to be neither (Chrome was
        # busy for that one instant), the navigation still didn't happen, so retry it.
        _check_tab()
        raise ExportError("the Netflix tab didn't take the navigation")
    if not stamped:
        time.sleep(3)
    _wait_progress(
        lambda: _run_main("o.alive=1;o.old=!!window.__gigaku_page;o.ready=document.readyState;"),
        lambda d: bool(d.get("alive")) and not d.get("old")
        and d.get("ready") in ("interactive", "complete"),
        lambda d: (d.get("alive"), d.get("old"), d.get("ready")),
        "page navigation", interval=0.5, giveup=40,
    )


def _goto(video_id: int) -> None:
    """Navigate the Netflix watch tab to a specific episode's watch page (by URL, no focus)."""
    global _WATCH_URL
    url = _WATCH_URL = f"https://www.netflix.com/watch/{video_id}"
    cur = _chrome_tab("return URL of t")
    if cur == "none":
        _check_tab()  # closed → stop; wandered off → steered back, then retried
        cur = _chrome_tab("return URL of t")
        if cur == "none":
            raise ExportError("no Netflix watch tab to navigate")
    # Already there (a retry, or the tab the user launched from): reload, so the document
    # turns over either way and `_navigate` can't wait for a navigation that never happens.
    _navigate("reload t" if cur and f"/watch/{video_id}" in cur else f'set URL of t to "{url}"')


def _reload_page() -> None:
    """Reload the Netflix watch tab — recover when Netflix/LR gets stuck loading (by URL)."""
    _navigate("reload t")


# ── Robust pause (background keeper) ──────────────────────────────────────────

# Resolve the player session for the CURRENT episode — the one in the URL — instead of blindly
# taking getAllPlayerSessionIds()[0]. Right after navigating to the next episode Netflix keeps
# the *previous* episode's session around for a while (and their order isn't stable), so [0]
# can point at a stale/closing player. That silently no-ops pause/play/seek (the episode won't
# stay paused) and makes the movie-id gate never match even though LR already loaded the right
# subs (the wait hangs). Injected at the top of a main-world body; leaves `api`, `sid`, `p`
# (the current VideoPlayer, or null) in scope.
_PLAYER = (
    "var api=netflix.appContext.state.playerApp.getAPI().videoPlayer;"
    "var _ids=api.getAllPlayerSessionIds()||[];var _i,sid=null;"
    "var _wm=(location.pathname.match(/watch\\/(\\d+)/)||[])[1];var _want=_wm?parseInt(_wm):null;"
    "for(_i=0;_i<_ids.length;_i++){try{"
    "if(api.isVideoPlayerClosedForSessionId&&api.isVideoPlayerClosedForSessionId(_ids[_i]))continue;"
    "if(_want!=null&&api.getVideoPlayerBySessionId(_ids[_i]).getMovieId()===_want){sid=_ids[_i];break;}"
    "}catch(e){}}"
    "if(sid===null){for(_i=_ids.length-1;_i>=0;_i--){try{"
    "if(!(api.isVideoPlayerClosedForSessionId&&api.isVideoPlayerClosedForSessionId(_ids[_i]))){sid=_ids[_i];break;}"
    "}catch(e){}}}"
    "if(sid===null&&_ids.length)sid=_ids[0];"
    "var p=sid?api.getVideoPlayerBySessionId(sid):null;"
)


def _pause() -> None:
    """Pause via both LR and the Netflix player (LR is glitchy — belt and suspenders)."""
    _run_main(
        "try{if(window.lln&&lln.vidMan)lln.vidMan.pause();}catch(e){}"
        "try{" + _PLAYER + "if(p)p.pause();}catch(e){}o.k=1;"
    )


# Nothing in the flow ever plays the video. An earlier version briefly played it to make LR
# generate fresh ASR subtitles, believing generation only progressed while the audio buffered.
# That is false: measured on a never-touched episode, holding the video force-paused at ct=0
# the whole time, LR still generated — 59 cues after 25s, 275 after a few minutes, all with
# paused=true and currentTime never leaving 0. Generation is server-side and progresses on its
# own. So the video stays paused from start to finish (see ``_pause_keeper``).


def _pause_keeper() -> None:
    """Daemon loop: while enabled, force the video paused every second. LR/Netflix like to
    resume playback on their own; this keeps it firmly paused except during ASR generation."""
    while True:
        if _PAUSE_KEEP.is_set():
            try:
                _pause()
            except Exception:  # noqa: BLE001 — tab may be gone; the main loop handles abort
                pass
        time.sleep(1.0)


# ── Season / episode discovery ───────────────────────────────────────────────

def _season_episodes() -> tuple[list[dict], int, str, int | None]:
    """Return (episodes ordered by seq, current video id, title, season number).

    For a series: the current season's episodes and its 1-based number. For a **movie** (no
    season list contains the current video): a single episode ``[{seq:1, id:cur, title}]``
    and ``season=None``.

    Everything here is **Netflix's** ``videoMetadata``, Language Reactor's ``mm.titles`` only
    as a fallback — which is what lets the run say which episodes it is about to download
    before it switches LR on. LR is the slow part (a cold toggle can sit at LOADING for
    minutes), and announcing the season only after that wait put the one thing the user is
    waiting to read behind the one thing they have to wait for.
    """
    d = _run_main(
        _PLAYER + "o.cur=p?p.getMovieId():_want;"
        "var mv=netflix.appContext.state.playerApp.getState().videoPlayer.videoMetadata;"
        "var vm=mv[Object.keys(mv)[0]];"
        # `_metadataObject.video.title`, NOT `_video.title` — that one is undefined here. Same
        # measured pair the chrome/gigaku extension reads the series name from.
        "try{o.series=vm._metadataObject.video.title||null;}catch(e){o.series=null;}"
        "try{if(!o.series){var t=lln.subManager.data.mm.titles;var k=Object.keys(t)[0];"
        "o.series=(t[k]&&t[k][0])?t[k][0]:null;}}catch(e){}"
        "o.seasons=[];var seasons=vm._seasons||[];"
        "for(var i=0;i<seasons.length;i++){var eps=seasons[i]._episodes||[];var arr=[];"
        "for(var j=0;j<eps.length;j++){var v=eps[j]._video;"
        "arr.push({seq:v.seq,id:v.episodeId,title:v.title});}o.seasons.push(arr);}"
    )
    if d.get("__err") or "cur" not in d:
        raise ExportError(f"could not read Netflix video metadata ({d})")
    cur = d["cur"]
    series = d.get("series") or "Netflix"
    for idx, season in enumerate(d["seasons"]):
        if any(e["id"] == cur for e in season):
            return sorted(season, key=lambda e: e["seq"]), cur, series, idx + 1
    # Not in any season → a movie: treat it as a single episode.
    return [{"seq": 1, "id": cur, "title": series}], cur, series, None


def _primary_srt(title_dir: Path, series: str, season: int | None, ep: dict) -> Path:
    """Where this episode's target-language SRT lands — and so whether it's already done."""
    return title_dir / f"{naming.srt_base(series, season, ep['seq'])} - Primary.srt"


# ── Language Reactor on/off ──────────────────────────────────────────────────

# LR's master switch for Netflix is the persisted ``NF_lingoActive`` setting, flipped by the
# ``#lln-toggle-btn`` button LR injects into the player. While it's off, LR never engages the
# video: ``subManager.data`` stays LOADING with no ``mm`` and every track step downstream
# fails. Clicking the toggle (not just poking the setting) is what fires LR's reactive load.

def _lln_is_on() -> bool | None:
    """True/False from LR's master switch; None if LR/the tab can't be read right now."""
    return _run_main("o.on=!!(window.lln&&lln.setMan&&lln.setMan.get('NF_lingoActive'));").get("on")


def _toggle_lln(target_on: bool) -> bool:
    """Click LR's on/off toggle until ``NF_lingoActive`` matches ``target_on``. Returns whether
    it ended in the wanted state; never raises (the tab may be gone — best-effort)."""
    for _ in range(8):
        cur = _lln_is_on()
        if cur is None:
            return False  # LR/tab unreachable
        if cur == target_on:
            return True
        try:
            clicked = _njs("var b=document.getElementById('lln-toggle-btn');"
                           "if(b){b.click();'ok';}else{'no-btn';}")
        except AppleScriptError:
            return False
        if clicked == "no-btn":
            # No toggle button right now (e.g. mid-reload) — flip the persisted setting
            # directly. Reliable for turning LR off; the on-path keeps retrying for the click.
            val = "true" if target_on else "false"
            _run_main("try{lln.setMan.set('NF_lingoActive'," + val + ");}catch(e){}o.k=1;")
        time.sleep(0.5)
    return _lln_is_on() == target_on


def _wait_lln_tracks() -> None:
    """Wait for LR to have this episode's track metadata (~15s from a cold toggle).

    Returns at once when they're already there. The 90s limit is deliberately long: while LR
    loads, the measured state is a *constant* ('LOADING', 0, false) — it exposes no
    intermediate progress — so the stall detector can't tell "slow" from "wedged", and any
    cold load slower than the limit is killed even though it would have finished. Measured
    LR taking over 2 minutes on a cold toggle, so patience here is cheaper than a false abort.
    """
    _wait_progress(
        lambda: _run_main(
            "try{var D=lln.subManager.data;o.state=D.state;o.n=D.subtitles?D.subtitles.length:0;"
            "o.mm=!!D.mm;o.ready=!!(D.mm&&D.state==='SUBS_LOADED'&&o.n>0);}"
            "catch(e){o.state='err';o.n=0;o.mm=false;o.ready=false;}"),
        lambda d: d.get("ready"), lambda d: (d.get("state"), d.get("n"), d.get("mm")),
        "Language Reactor track load", interval=1.0, giveup=90,
        log=lambda d: print(f"    …LR loading (state={d.get('state')} n={d.get('n')})"),
    )


def _ensure_lln_on(reloads: int = 3) -> None:
    """Turn Language Reactor on if the user has it toggled off, then wait for it to load the
    episode's tracks. Raises ``ExportError`` if the switch won't flip — nothing downstream can
    read audio/subtitle tracks without it.

    Waits for the tracks whenever they aren't loaded — not only when it had to flip the
    toggle. LR being *on* says nothing about whether it has engaged: a run stopped with Ctrl-C
    leaves the switch on with ``subManager.data`` still at LOADING, and returning early there
    failed preflight instantly with "make sure the episode is actually playing", which is not
    the problem and not something the user can act on.

    Switched on mid-page LR can also sit at LOADING for minutes. ``NF_lingoActive`` is
    persisted, so a reload brings the page up with LR already on and it engages from the start
    — reload-and-retry, the same recovery the rest of the flow uses. This is the *normal*
    path, not an edge case: every run ends by switching LR off, so every run begins by
    switching it back on.
    """
    if not _lln_is_on():
        print("  Language Reactor is off — turning it on…")
        if not _toggle_lln(True):
            raise ExportError(
                "couldn't turn Language Reactor on — its on/off toggle didn't respond.")
    for attempt in range(1, reloads + 1):
        try:
            _wait_lln_tracks()
            return
        except ExportError:
            if attempt >= reloads:
                raise
            print("  LR didn't load its tracks — reloading the page")
            _reload_page()


# ── Language Reactor state ───────────────────────────────────────────────────

_SUBS_STATE = (
    "o.hasLln=(typeof window.lln!=='undefined');o.mid=null;o.ct=null;o.paused=null;o.tmType=null;"
    "try{var v=document.querySelector('video');if(v){o.ct=Math.round(v.currentTime);o.paused=v.paused;}}catch(e){}"
    "try{" + _PLAYER + "if(p){o.mid=p.getMovieId();if(o.paused===null)o.paused=p.getPaused();}}catch(e){}"
    "if(o.hasLln&&lln.subManager&&lln.subManager.data){var D=lln.subManager.data;"
    "o.state=D.state;o.n=D.subtitles?D.subtitles.length:0;o.tmType=D.tm?D.tm.type:null;"
    "o.tmName=D.tm?D.tm.name:null;}else{o.state='no-lln';o.n=0;}"
)


def _wait_subs(expect_id: int | None = None, generating: bool = False,
               want_type: str | None = None, giveup: float | None = None,
               renudge=None, want_name: str | None = None) -> dict:
    """Adaptively wait until LR's subtitles are loaded (for ``expect_id`` if given).

    If ``want_type`` is set (e.g. ``'asr'``) it also requires the current text track to be
    that type — so it won't accept a silent fallback to the official track. Progress = the
    subtitle state/count/track-type changing (NOT the video's currentTime, which advances
    even while a wrong track is stuck loading). ``giveup`` overrides the stall limit.

    ``renudge`` re-applies the track selection. LR does not merely fail to switch — it
    switches, then *reverts* to the title's default track seconds later, leaving a settled
    (SUBS_LOADED, stable count, wrong type) state that no amount of waiting will move. Only
    re-clicking gets out of it, so a stall on the wrong track re-applies instead of dying.
    """
    def wrong_track(d):
        """The loaded track isn't the one asked for — by type *or* by name."""
        if d.get("tmType") is None:
            return False  # nothing loaded yet; not a drift
        if want_type is not None and d.get("tmType") != want_type:
            return True
        return bool(want_name) and (d.get("tmName") or "").lower() != want_name.lower()

    def done(d):
        if not (d.get("state") == "SUBS_LOADED" and d.get("n", 0) > 0
                and (expect_id is None or d.get("mid") == expect_id)):
            return False
        if want_type is not None:
            return d.get("tmType") == want_type and not wrong_track(d)
        return d.get("tmType") is not None  # track metadata ready before we touch it

    def on_stall(d):
        # Only re-click when the track actually drifted; mid-generation the type is already
        # right (state SUBS_LOADING) and re-selecting would restart the generation.
        if renudge and wrong_track(d):
            print(f"    track drifted to {d.get('tmType')}:{d.get('tmName')} — "
                  f"re-selecting {want_type}:{want_name}")
            renudge()

    return _wait_progress(
        lambda: _run_main(_SUBS_STATE), done,
        lambda d: (d.get("state"), d.get("n"), d.get("mid"), d.get("tmType")),
        "subtitles load", on_stall=on_stall, giveup=giveup,
        log=lambda d: print(f"    …{'generating ASR' if generating else 'loading subs'} "
                            f"(state={d.get('state')} n={d.get('n')} tm={d.get('tmType')} ct={d.get('ct')})"),
    )


def _click_tracks(aid: str, sid: str) -> None:
    """(Re-)select the audio + subtitle track by id — LR's own setters, no menu clicking."""
    _run_main(f"lln.vidMan.setAudioTrack({json.dumps(aid)});"
              f"lln.vidMan.setSubtitleTrack({json.dumps(sid)});o.k=1;")


def _set_tracks(audio: str, sub_name: str, sub_is_asr: bool) -> tuple[str, str]:
    """Select the audio + (ASR or official) subtitle track via LR; return their track ids so
    the caller can re-apply them when LR reverts (see ``_wait_subs``'s ``renudge``).

    LR is glitchy: a single ``setSubtitleTrack`` often doesn't take, so click it several
    times and REQUIRE the current text track to flip to the wanted type; if it won't, raise
    so the caller reloads and retries."""
    find = (
        f"var AUDIO={json.dumps(audio)};var SUB={json.dumps(sub_name)};"
        f"var ASR={'true' if sub_is_asr else 'false'};"
        "function ci(s){return (''+(s||'')).toLowerCase();}"
        "var mm=lln.subManager.data.mm;"
        "o.audioAvail=mm.audioTracks.map(function(t){return t.name;});"
        "o.subAvail=mm.textTracks.map(function(t){return t.type+':'+t.name;});"
        "var a=mm.audioTracks.filter(function(t){return ci(t.name)===ci(AUDIO);})[0];"
        "var sub=mm.textTracks.filter(function(t){return t.type===(ASR?'asr':'subtitles')&&ci(t.name)===ci(SUB);})[0];"
        "o.audioId=a?a.new_track_id:null;o.subId=sub?sub.new_track_id:null;"
    )
    d = _run_main(find)
    if d.get("__err"):
        raise ExportError(f"setting tracks failed: {d['__err']}")
    if not d.get("audioId"):
        raise ExportError(f"audio track '{audio}' not found; available: {d.get('audioAvail')}")
    if not d.get("subId"):
        kind = "ASR" if sub_is_asr else "official"
        raise ExportError(f"{kind} subtitle '{sub_name}' not found; available: {d.get('subAvail')}")
    aid, sid = d["audioId"], d["subId"]
    want = "asr" if sub_is_asr else "subtitles"
    chk = {}
    for attempt in range(1, 11):
        _click_tracks(aid, sid)
        time.sleep(0.5)
        chk = _run_main("var tm=(window.lln&&lln.subManager)?lln.subManager.data.tm:null;"
                        "o.type=tm?tm.type:null;o.name=tm?tm.name:null;")
        # The NAME has to match too. Asked for ASR German, LR will happily land on ASR
        # "Korean [Original]" — same type, wrong language — and a type-only check calls that
        # success, then burns a whole load attempt before the hard gate rejects it.
        got_name = (chk.get("name") or "").lower() == sub_name.lower()
        if chk.get("type") == want and got_name and attempt >= 3:  # click 3× before trusting it
            print(f"  set audio={audio}  subtitle={chk.get('type')}:{chk.get('name')} ({attempt} clicks)")
            return aid, sid
    raise ExportError(f"subtitle track wouldn't switch to '{want}' after 10 clicks "
                      f"(tm={chk.get('type')}:{chk.get('name')})")


def _verify_tracks(audio: str, sub_name: str, sub_is_asr: bool) -> dict:
    """Read back the *actually active* audio + subtitle tracks and check they match — the
    hard gate before exporting. LR silently falls back to the official track when the ASR
    switch doesn't take, so never trust the set; verify."""
    want = "asr" if sub_is_asr else "subtitles"
    return _run_main(
        "function ci(s){return (''+(s||'')).toLowerCase();}"
        f"var A={json.dumps(audio)};var S={json.dumps(sub_name)};var T={json.dumps(want)};"
        "var D=(window.lln&&lln.subManager)?lln.subManager.data:null;"
        "if(!D){o.ok=false;o.why='no-lln';}else{var am=D.am,tm=D.tm;"
        "o.state=D.state;o.n=D.subtitles?D.subtitles.length:0;"
        # Where the subtitles end — the export must reach it (LR merges cues into sentence
        # rows, so the row *count* says nothing about completeness; the last cue does).
        "o.lastMs=(D.subtitles&&D.subtitles.length)?D.subtitles[D.subtitles.length-1].begin:null;"
        # ...but "the last cue" is not "the end of the episode". ASR keeps transcribing over
        # the END CREDITS — the closing song, in whatever language it is sung — while LR's
        # export stops with the programme, so a complete export reads as three to five
        # minutes short and was rejected. Netflix knows exactly where that line is, in the
        # metadata this page has already loaded: `creditsOffset`, the seconds at which the
        # "Skip Credits" button appears. Measured 2026-09-03 — Elite E01 exported to 52:04
        # against a creditsOffset of 52:36 and cues to 55:59; The East Palace E01 to 41:14
        # against 41:24 and cues to 45:29. The export ends at the credits, to the second.
        "o.creditsMs=null;"
        "try{var MV=netflix.appContext.state.playerApp.getState().videoPlayer.videoMetadata;"
        "var _want=(location.pathname.match(/watch\\/(\\d+)/)||[])[1];"
        "var _mk=Object.keys(MV);"
        "for(var a=0;a<_mk.length&&o.creditsMs===null;a++){"
        "var _v=MV[_mk[a]]._metadataObject?MV[_mk[a]]._metadataObject.video:null;"
        "var _ss=(_v&&_v.seasons)||[];"
        "for(var b=0;b<_ss.length&&o.creditsMs===null;b++){var _es=_ss[b].episodes||[];"
        "for(var c=0;c<_es.length;c++){if(String(_es[c].id)===String(_want)&&_es[c].creditsOffset){"
        "o.creditsMs=_es[c].creditsOffset*1000;break;}}}}"
        "}catch(e){}"
        # Fallback for whatever has no such marker (a film, a metadata shape that moved): the
        # last cue before the final long silence. It is a guess — Elite's closing song pauses
        # for at most 52s, so this one would have kept rejecting it — but it beats nothing.
        "o.bodyMs=o.lastMs;"
        "try{var Q=D.subtitles||[],V=document.querySelector('video'),"
        "DUR=(V&&V.duration&&isFinite(V.duration))?V.duration*1000:0;"
        f"var GAP={CREDITS_GAP_MS};"
        "for(var i=Q.length-1;i>0;i--){var g=Q[i].begin-Q[i-1].begin;"
        "if(g>=GAP&&(!DUR||Q[i-1].begin>=0.75*DUR)){o.bodyMs=Q[i-1].begin;break;}}"
        "}catch(e){}"
        "o.audioName=am?am.name:null;o.tmType=tm?tm.type:null;o.tmName=tm?tm.name:null;"
        "o.audioOk=!!(am&&ci(am.name)===ci(A));o.subOk=!!(tm&&tm.type===T&&ci(tm.name)===ci(S));"
        "o.ok=(D.state==='SUBS_LOADED'&&o.n>0&&o.audioOk&&o.subOk);}"
    )


# ASR arrives PROGRESSIVELY: LR flips state to SUBS_LOADED as soon as the first cues land, not
# when the last one does (measured: 59 cues after 25s, 275 after minutes, on one episode). So
# "loaded" is not "complete", and exporting on it wrote a 45-cue, 9-minute file for a 50-minute
# episode — which passed every other check, because they compare the export against LR's own cue
# list and that list was itself truncated. The only reference outside LR is the video's own
# duration: real subtitles run to somewhere near the end of the episode.
ASR_MIN_COVERAGE = 0.7

# Ticks (of _wait_progress's 2s) the cue count must sit still before the episode is exported
# — see `_wait_asr_complete`.
ASR_STABLE_TICKS = 4

# A silence this long inside the last quarter of an episode is taken for its end credits, and
# the ASR cues after it for the closing song rather than the programme. Only used where
# Netflix offers no `creditsOffset` (see `_verify_tracks`), which is the real measurement.
CREDITS_GAP_MS = 90_000

# LR generates ASR on a shared backend and QUEUES it, and a list-long rip fills that queue:
# generation stops adding cues and LR says "Problem fetching ASR subs. QUEUE_FULL" (seen
# 2026-09-03, an hour into ripping a 30-title list). The reflex for a stall — reload the page
# and ask again — answers a full queue with more of exactly what it refused, so it is waited
# out instead, the same way the translation rate limit is.
ASR_QUEUE_BACKOFF = 120     # seconds to sit still before looking again
ASR_QUEUE_MAX_WAITS = 10    # ~20 minutes, then the episode fails and a re-run resumes it

_COVERAGE = (
    "var D=lln.subManager.data;o.n=D.subtitles?D.subtitles.length:0;"
    "o.lastMs=(D.subtitles&&D.subtitles.length)?D.subtitles[D.subtitles.length-1].begin:null;"
    "o.state=D.state;o.tmType=D.tm?D.tm.type:null;o.tmName=D.tm?D.tm.name:null;"
    "try{var v=document.querySelector('video');"
    "o.durMs=(v&&v.duration&&isFinite(v.duration))?Math.round(v.duration*1000):null;}catch(e){}"
)


# LR does not merely fail to switch a track — it switches, then *reverts* to the title's
# default on its own clock (see ``_wait_subs``). The stretch between loading the track and
# exporting is the longest part of an episode — ASR coverage, then thousands of machine
# translations, then rate-limit backoff — so a revert lands inside it routinely, and it used
# to be noticed only by the verify *after* all of that ("tracks drifted to
# closedcaptions:Korean before export"). By then LR had fetched a translation for every line
# of the *wrong* track, all of it thrown away, and the re-select paid for the whole wait a
# second time. So the long waits check the track on every tick instead.
#
# Two things this has to get right. It cannot ride on ``on_stall``: while LR sits on the wrong
# track its counts climb perfectly happily — it is busy translating Korean — so nothing stalls.
# And it has to be **bounded**: re-selecting forever against an LR that reverts every few
# seconds would never stall either (an oscillating count reads as progress), so after
# ``DRIFT_LIMIT`` repairs the episode gives up to the retry loop, which reloads the page.
DRIFT_LIMIT = 5


class _TrackWatch:
    """Notices LR reverting the subtitle track mid-wait, and re-selects on the spot."""

    def __init__(self, aid: str, sid: str, sub_name: str, sub_is_asr: bool):
        self.aid, self.sid = aid, sid
        self.name = sub_name
        self.want = "asr" if sub_is_asr else "subtitles"
        self.repairs = 0

    def drifted(self, d: dict) -> bool:
        """Is the loaded track something other than the one asked for? A track that isn't
        loaded *yet* is not a drift — that is just LR still working."""
        if d.get("tmType") is None:
            return False
        return (d.get("tmType") != self.want
                or (d.get("tmName") or "").lower() != self.name.lower())

    def guard(self, d: dict) -> None:
        """Per-tick repair, for ``_wait_progress(on_measure=…)``."""
        if not self.drifted(d):
            return
        self.repairs += 1
        if self.repairs > DRIFT_LIMIT:
            raise ExportError(
                f"LR keeps reverting the subtitle track (now {d.get('tmType')}:"
                f"{d.get('tmName')}) — re-selected {DRIFT_LIMIT} times and it won't hold")
        print(f"    track reverted to {d.get('tmType')}:{d.get('tmName')} — "
              f"re-selecting {self.want}:{self.name} ({self.repairs}/{DRIFT_LIMIT})")
        _click_tracks(self.aid, self.sid)


def _wait_asr_complete(watch: _TrackWatch, ratio: float = ASR_MIN_COVERAGE) -> dict:
    """Wait until the ASR cues actually reach the end of the episode, not just the start.

    Every new cue counts as progress, so this rides out a slow generation and only gives up
    when the count genuinely plateaus short of the episode — an incomplete rip that would
    otherwise be exported and silently accepted. Coverage is only believed while the wanted
    track is loaded: the title's default track is complete from the first second, so a revert
    would otherwise satisfy this instantly and hand back a "finished" episode of Korean.
    """
    # Coverage is a floor, not the finish line: 70% of the episode is where generation stops
    # being *useless*, not where it is done, and the export writes the cues that exist when
    # the click lands. So the wait also asks the count to sit still for a few ticks. (This is
    # not what caused the "stops at …" rejections of 2026-09-03 — those were the end credits,
    # see `_verify_tracks` — the rule is simply true on its own: a count still climbing is an
    # episode still being transcribed.)
    stable = {"n": None, "hits": 0}

    def done(d):
        dur, last, n = d.get("durMs"), d.get("lastMs"), d.get("n")
        if not dur or not last or watch.drifted(d):
            stable["n"], stable["hits"] = None, 0
            return False
        if last < ratio * dur:
            return False
        if n == stable["n"]:
            stable["hits"] += 1
        else:
            stable["n"], stable["hits"] = n, 0
        return stable["hits"] >= ASR_STABLE_TICKS

    waits = 0

    def on_stall(d):
        """A generation that stopped because LR's queue is full is not a stall to reload."""
        nonlocal waits
        if not _asr_queue_full():
            return False
        waits += 1
        if waits > ASR_QUEUE_MAX_WAITS:
            return False  # waited long enough — let the episode fail; a re-run resumes
        print(f"    LR's ASR queue is full — waiting {ASR_QUEUE_BACKOFF}s "
              f"({waits}/{ASR_QUEUE_MAX_WAITS})")
        _sleep_abortable(ASR_QUEUE_BACKOFF)
        return True  # a deliberate wait, not a stall

    return _wait_progress(
        lambda: _run_main(_COVERAGE), done, lambda d: d.get("n"),
        "ASR generation reaching the end of the episode", giveup=180,
        on_measure=watch.guard, on_stall=on_stall,
        log=lambda d: print(
            f"    …ASR {d.get('n')} cues, covers "
            f"{(d.get('lastMs') or 0) / (d.get('durMs') or 1) * 100:.0f}% of the episode"),
    )


def _get_autopause() -> bool | None:
    return _run_main("o.ap=lln.setMan.get('autoPause');").get("ap")


def _set_autopause(value: bool) -> None:
    _run_main(f"try{{lln.setMan.set('autoPause',{'true' if value else 'false'});}}catch(e){{}}o.k=1;")


def _seek_start() -> None:
    """Rewind the episode to the very beginning (before and after each export)."""
    _run_main(
        "try{" + _PLAYER + "if(p)p.seek(0);}catch(e){}"
        "try{if(window.lln&&lln.vidMan&&lln.vidMan.seek)lln.vidMan.seek(0);}catch(e){}o.k=1;"
    )


_MT_STATE = ("var D=lln.subManager.data;o.n=D.subtitles?D.subtitles.length:0;"
             "o.mt=D.mTranslations?Object.keys(D.mTranslations).length:0;"
             # The track comes along so a revert is caught on the tick it happens, rather than
             # after every line of the wrong track has been translated (see _TrackWatch).
             "o.tmType=D.tm?D.tm.type:null;o.tmName=D.tm?D.tm.name:null;")

# Translating a whole season is thousands of lines, and LR's translation backend rate-limits:
# it stops filling mTranslations and shows "Problem fetching translations. RATE_LIMIT_EXCEEDED".
# Reloading and re-requesting — the flow's reflex for a stall — answers a rate limit with *more*
# requests, which is exactly the wrong move. So the limit is recognised and waited out instead.
RATE_LIMIT_BACKOFF = 90    # seconds to sit still before asking again
RATE_LIMIT_MAX_WAITS = 8   # ~12 min of backoff, then let the episode fail (a re-run resumes)


def _lr_problem(pattern: str) -> bool:
    """Is LR showing a problem toast matching ``pattern`` right now?

    Clears the toast after reading it, so the next check sees a fresh error rather than a
    stale one from several minutes ago and backs off forever over an error already past.
    """
    try:
        text = _njs(
            "var n=document.querySelector('.lln-notification-content');"
            "var t=n?n.textContent.trim():'';"
            "if(/RATE_LIMIT|QUEUE_FULL|Problem fetching/i.test(t)){"
            "var b=document.querySelector('.lln-notifications');if(b)b.textContent='';}"
            "t;"
        ) or ""
    except AppleScriptError:
        return False
    return bool(re.search(pattern, text, re.I))


def _translation_rate_limited() -> bool:
    return _lr_problem(r"RATE_LIMIT|Problem fetching translations")


def _asr_queue_full() -> bool:
    """LR's ASR backend refusing more work: "Problem fetching ASR subs. QUEUE_FULL"."""
    return _lr_problem(r"QUEUE_FULL|Problem fetching ASR")


def _sleep_abortable(seconds: float) -> None:
    """Sleep, but keep noticing a closed tab / Ctrl-C."""
    end = time.time() + seconds
    while time.time() < end:
        _check_tab()
        time.sleep(2)


def _wait_translations(watch: _TrackWatch) -> dict:
    """Adaptively wait until LR has a machine translation cached for every line. Returns
    almost immediately if already complete; otherwise nudges LR once, then waits.

    ``watch`` re-selects the track the moment LR reverts it. Without that, this is the wait
    that used to burn: a revert here means LR happily translates the whole of the *default*
    track — nothing stalls, the counts climb, it reads as complete — and only the verify
    afterwards noticed, at the price of every one of those translations.

    Completion must hold on **two consecutive** readings. ``mt >= n`` compares two counts LR
    rewrites independently, and LR rebuilds ``subtitles`` mid-episode (it reverts tracks — see
    ``_wait_subs``), so a single reading can be taken while ``n`` dips and ``mt`` still counts
    the previous track's translations. Exporting on that reading would write only the
    translated prefix. Defensive: never observed truncating an export, but the window is real
    and a second reading two seconds later costs nothing.
    """
    hits = 0

    def complete(d):
        return (d.get("n", 0) > 0 and d.get("mt", 0) >= d.get("n", 0)
                and not watch.drifted(d))  # complete for the wrong track is not complete

    def done(d):
        nonlocal hits
        hits = hits + 1 if complete(d) else 0
        return hits >= 2

    nudge = ("if(window.lln&&lln.subManager&&lln.subManager.setupMTranslations)"
             "{lln.subManager.setupMTranslations();}o.k=1;")
    # The opening probe is guarded too: a revert that landed between loading the track and
    # getting here would otherwise go unrepaired until the first tick of the loop, with the
    # nudge below asking LR to translate the wrong track in the meantime.
    first = _run_main(_MT_STATE)
    watch.guard(first)
    if not complete(first):
        _run_main(nudge)

    waits = 0

    def on_stall(d):
        nonlocal waits
        if not _translation_rate_limited():
            return False
        waits += 1
        if waits > RATE_LIMIT_MAX_WAITS:
            return False  # waited long enough — let it fail, the episode retries/resumes
        print(f"    LR rate-limited translations — waiting {RATE_LIMIT_BACKOFF}s "
              f"({waits}/{RATE_LIMIT_MAX_WAITS})")
        _sleep_abortable(RATE_LIMIT_BACKOFF)
        _run_main(nudge)
        return True  # a deliberate wait, not a stall

    return _wait_progress(
        lambda: _run_main(_MT_STATE), done, lambda d: d.get("mt"),
        "machine translations", on_measure=watch.guard, on_stall=on_stall,
        log=lambda d: print(f"    …translating {d.get('mt')}/{d.get('n')}"),
    )


# ── Export dialog (plain DOM) ────────────────────────────────────────────────

def _xlsx_names() -> set[str]:
    return {p.name for p in Path(CHROME_DOWNLOAD_DIR).glob("*.xlsx")}


def _set_overlay(hidden: bool) -> None:
    """Hide/restore Migaku's shadow-DOM overlay so it can't cover the export modal."""
    val = "none" if hidden else ""
    _njs(f"var m=document.getElementById('MigakuShadowDom');if(m){{m.style.display='{val}';}}'ok';")


# Open the modal, select the Excel tab, set the toggles. Idempotent, so it's safe to re-run
# each poll until the Excel tab has a visible Export button and the machine-translation
# checkbox in the state this run wants.
# Visibility is via getBoundingClientRect — NOT offsetParent, which is null for the children
# of a position:fixed modal even when fully visible.
def _export_prep(want_mt: bool) -> str:
    """The idempotent "get the export dialog ready" script, for one MT setting.

    ``want_mt`` is False whenever the Secondary track is going to be glossed from the German
    afterwards (lib/subs/translate.py), which is the default. That is not a cosmetic
    difference: LR fetches those translations line by line and rate-limits a season while
    doing it, so leaving the box *unticked* is what removes the slowest and most failure-prone
    stretch of a rip. Ticked, everything behaves exactly as it always did.
    """
    want = "true" if want_mt else "false"
    return (
        "function vis(el){if(!el)return false;var r=el.getBoundingClientRect();return r.width>0&&r.height>0;}"
        "var b=document.querySelector('.lln-open-export-modal');"
        "var m=document.querySelector('.lln-export-modal-content');"
        "if(!m||!vis(m)){if(b)b.click();'opening';}else{"
        "var ts=m.querySelectorAll('.lln-tab-title');"
        "for(var i=0;i<ts.length;i++){if(ts[i].textContent.trim()==='Excel'&&!/active/.test(ts[i].className))ts[i].click();}"
        f"var want={want};"
        "var mt=m.querySelector('input[name=includeMachineTranslations]');"
        "if(mt&&mt.checked!==want)mt.click();"
        "var it=m.querySelector('input[name=includeTimestamps]');if(it&&!it.checked)it.click();"
        "var hw=m.querySelector('input[name=highlightSavedWords]');if(hw&&!hw.checked)hw.click();"
        "var eb=null,bs=m.querySelectorAll('.lln-btn');"
        "for(var j=0;j<bs.length;j++){if(bs[j].textContent.trim()==='Export')eb=bs[j];}"
        "(eb&&vis(eb)&&mt&&mt.checked===want)?'ready':'preparing';}"
    )


# LR's OWN precondition for the Excel export, read out of its bundle rather than guessed:
#
#     if ("SUBS_LOADED" !== data.state || !data.tm.langCode_G || !data.nlp)
#         return void alert("Subtitles not loaded, no data to export.");
#
# ``_verify_tracks`` answers the first of the three (plus the track names) and cannot see the
# other two, which is how a finished episode — right track, 416 cues, every line translated —
# still met that refusal: ``nlp`` is LR's parse of the loaded track, and it is rebuilt after a
# track change or the rewind this flow does immediately before exporting, so there is a window
# where LR has the subtitles and is not yet willing to export them. Waiting on LR's own three
# conditions closes it exactly, and needs no theory about which one was missing.
_EXPORTABLE = (
    "var D=(window.lln&&lln.subManager)?lln.subManager.data:null;"
    "o.state=D?D.state:null;o.lang=!!(D&&D.tm&&D.tm.langCode_G);o.nlp=!!(D&&D.nlp);"
    "o.ok=!!(o.state==='SUBS_LOADED'&&o.lang&&o.nlp);"
)

# Seconds of a settled, still-not-exportable LR before the episode goes back to the retry loop
# (which reloads). Generous: the parse is rebuilt in seconds when it is going to be rebuilt.
EXPORT_READY_GIVEUP = 60


def _export(want_mt: bool = True) -> str:
    """Run LR's Excel export; return the downloaded filename.

    ``want_mt`` decides whether the Machine Translation column is asked for at all.
    """
    _wait_progress(
        lambda: _run_main(_EXPORTABLE), lambda d: bool(d.get("ok")),
        lambda d: (d.get("state"), d.get("lang"), d.get("nlp")),
        "LR ready to export", interval=1.0, giveup=EXPORT_READY_GIVEUP,
        log=lambda d: print(f"    …waiting for LR to be exportable "
                            f"(state={d.get('state')} lang={d.get('lang')} nlp={d.get('nlp')})"),
    )
    _dialog(clear=True)   # start from a clean slate, so a refusal read below is this click's
    _set_overlay(True)
    try:
        _wait_progress(
            lambda: {"r": _njs(_export_prep(want_mt))}, lambda d: d.get("r") == "ready",
            lambda d: d.get("r"), "export dialog ready", interval=0.6, giveup=25,
        )
        before = _xlsx_names()
        clicked = _njs(
            "var m=document.querySelector('.lln-export-modal-content');var r='no-btn';"
            "var b=m.querySelectorAll('.lln-btn');"
            "for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Export'){b[i].click();r='ok';break;}}r;"
        )
        if clicked != "ok":
            raise ExportError("Export button inside the dialog not found")
        # LR refuses synchronously — the alert is the first statement of its click handler —
        # so this is not a race, and it is the answer to a download that will never start.
        refused = _dialog(clear=True)
        if refused:
            raise ExportError(f"Language Reactor refused the export: {refused}")

        try:
            new = _wait_progress(
                lambda: {"new": sorted(_xlsx_names() - before)},
                lambda d: bool(d["new"]), lambda d: len(d["new"]),
                "export .xlsx download", interval=1.0,
            )["new"][0]
        except ExportError:
            # LR's later refusals ("Error preparing export data…", "Export error: …") arrive
            # once the click has been accepted, so they read as a download that never came.
            # Its own sentence says more than that.
            late = _dialog(clear=True)
            if late:
                raise ExportError(f"Language Reactor failed the export: {late}") from None
            raise
        _njs("var c=document.querySelector('.lln-export-modal-content .lln-close-modal');if(c)c.click();'ok';")
        return new
    finally:
        _set_overlay(False)


# ── Orchestration ────────────────────────────────────────────────────────────

def _parse_subtitle(spec: str) -> tuple[str, bool]:
    """'ASR Pro German' -> ('German', True); 'German' -> ('German', False)."""
    is_asr = "asr" in spec.lower()
    name = spec
    for tok in ("ASR", "Pro", "asr", "pro"):
        name = name.replace(tok, "")
    return name.strip(), is_asr


def _reassert_tracks(aid: str, sid: str, audio: str, sub_name: str,
                     sub_is_asr: bool, tries: int = 6) -> dict:
    """Re-select the wanted tracks and wait for LR to actually show them again.

    LR reverts the track on its own clock, and the gap between loading it and exporting is
    long (ASR coverage, then thousands of machine translations, then rate-limit backoff), so
    a revert lands there routinely. Re-applying costs seconds; treating it as fatal threw
    away an episode that was one click from being exportable.
    """
    v: dict = {}
    for _ in range(tries):
        _click_tracks(aid, sid)
        time.sleep(1.5)
        v = _verify_tracks(audio, sub_name, sub_is_asr)
        if v.get("ok"):
            return v
    return v


def _load_track(ep: dict, audio: str, sub_name: str,
                sub_is_asr: bool, reloads: int = 4) -> tuple[str, str]:
    """Load the episode and get the requested track actually loaded, reloading the page to
    recover if Netflix/LR gets stuck or silently falls back to the wrong track. Verifies the
    loaded track type so an ASR request never quietly exports the official subtitles."""
    want_type = "asr" if sub_is_asr else "subtitles"
    for attempt in range(1, reloads + 1):
        try:
            if attempt == 1:
                _goto(ep["id"])   # navigate by URL (no focus stealing), wait for the new doc
            print("  loading subtitles…")
            # 25s, not 12: the wait now starts on a genuinely fresh document (see _navigate),
            # where LR needs a good while to appear — reloading under it just restarts the wait.
            _wait_subs(expect_id=ep["id"], giveup=25)   # page + official subs
            aid, sid = _set_tracks(audio, sub_name, sub_is_asr)  # LR glitchy — clicks several times
            # A cached ASR track is there at once; a fresh one has to be generated. Either way
            # the video STAYS PAUSED — generation is server-side and needs no playback.
            cached = False
            for _ in range(3):
                _pause()
                if _verify_tracks(audio, sub_name, sub_is_asr).get("ok"):
                    cached = True
                    break
                time.sleep(1)
            if not cached:
                print("  generating ASR (video stays paused)…")
                # Generous limit: generation trickles in server-side (measured ~25s to the
                # first cues, minutes to the full episode). Every new cue counts as progress,
                # so a real stall still trips the detector — this only buys patience.
                _wait_subs(generating=True, want_type=want_type, want_name=sub_name,
                           giveup=120, renudge=lambda: _click_tracks(aid, sid))
                _pause()
            v = _verify_tracks(audio, sub_name, sub_is_asr)  # hard gate — never trust the set
            if not v.get("ok"):
                raise ExportError(
                    f"tracks wrong (audio={v.get('audioName')} sub={v.get('tmType')}:"
                    f"{v.get('tmName')} state={v.get('state')})")
            print(f"  ✓ verified audio={v.get('audioName')}  "
                  f"subtitle={v.get('tmType')}:{v.get('tmName')}  n={v.get('n')}")
            return aid, sid
        except (ExportError, AppleScriptError) as e:
            if attempt >= reloads:
                raise
            print(f"  load attempt {attempt}: {e} — reloading page")
            _reload_page()


# Episodes whose Primary landed but whose gloss didn't finish. Reported at the end of the
# run and left for `gigaku translate` — never retried here, because re-ripping an episode
# that is already on disk is the one thing a translation failure must not cause.
_UNGLOSSED: list[str] = []


class WrongTrack(ExportError):
    """The export came back in a language the requested track cannot be written in.

    Its own class because it has its own cure: a plain reload has been *measured* not to clear
    it (E12 failed three attempts, each with a `_reload_page()` between them), while a run that
    switched Language Reactor off and on came back right on its first attempt. Every other
    ``ExportError`` here is cured by reloading and trying again.
    """


def _restart_lln() -> None:
    """Switch Language Reactor off and on again. Best-effort — never worth failing over."""
    try:
        _toggle_lln(False)
        time.sleep(1)
        _toggle_lln(True)
    except (AppleScriptError, ExportError) as e:
        print(f"  (couldn't restart Language Reactor: {e})")


def _recover_episode(exc: Exception) -> None:
    """What happens between two attempts at the same episode.

    Everything gets a reload. A wrong-track export gets Language Reactor restarted *first*,
    because that is the one thing observed to change the outcome — `NF_lingoActive` is
    persisted, so the reload below then brings LR up fresh and engaging from the start, which
    is the shape the successful run had. **The evidence is one observation**, so this is
    deliberately bounded: it costs a couple of seconds, it cannot make a good export bad, and
    the next attempt is judged by the same language check as the last one. If a second
    measurement ever contradicts it, delete the branch and nothing else changes.
    """
    if isinstance(exc, WrongTrack):
        print("  restarting Language Reactor — a reload alone has been measured not to "
              "clear a wrong-track export")
        _restart_lln()
    _reload_page()


def _index(subs_root: Path, ep: dict, series: str, season: int) -> None:
    """Record this episode in the extension's index — and only once the PAIR exists.

    ``library.scan`` indexes a Primary+Secondary pair and nothing else, and ``library.update``
    drops any id whose episode the scan didn't find. So this must run *after* the Secondary is
    written: called between the two, it silently loses the Netflix id, which is the exact,
    locale-proof match the extension prefers over title+season+episode. That is precisely what
    the parallel glosser caused — five episodes indexed by title with no id at all.
    """
    try:
        library.update(str(subs_root), ids={ep["id"]: (series, season, ep["seq"])})
    except OSError as e:
        print(f"  (subtitle index not updated: {e})")


def _gloss(primary: Path, secondary: Path, tag: str = "") -> bool:
    """Write the Secondary by glossing the Primary. Never raises.

    Imported at call time, like every other domain module here: `gigaku subs` should not pay
    for the translator's imports on a run with SUBS_TRANSLATE off, and the translator must
    not drag in pyobjc when `gigaku translate` uses it on its own.

    ``tag`` names the episode on every line this produces, because it no longer produces them
    alone — see ``_Glosser``.

    **The German is proofread first, and this is the only place it happens during a rip.**
    The Primary is ASR, so the Russian would otherwise be glossed from the transcriber's
    guesses (`lib/subs/spell.py`). Three things that placement settles at once. It is off the
    browser's critical path, because this whole function already runs on `_Glosser`'s worker —
    putting it in `_export_episode` instead would hand the ~1 minute back to the sum the
    pipeline exists to avoid. It cannot re-rip an episode, because it is outside
    `_export_episode`'s attempt loop: the Primary is already on disk and a `claude` that is
    missing must not send the run back to Language Reactor. And `--no-translate` gets "no
    proofreading" **structurally** — this function is only reached under `if glossing:`, so
    the path documented as needing no `claude` on PATH keeps needing none, with no second
    switch to drift out of step. `spell.episode` never raises; a failure leaves the German
    exactly as it was ripped and says so.
    """
    from lib.subs import translate

    with noting(tag):
        if settings.SUBS_SPELL:
            from lib.subs import spell

            spell.episode(primary)
        try:
            stats = translate.episode(primary, secondary)
        except (translate.TranslateError, UserError) as e:
            _UNGLOSSED.append(primary.name)
            note(f"✗ Secondary not written: {e}")
            note("(the Primary is safe — run `gigaku translate` to finish it)")
            return False
        note(f"✓ wrote {secondary.name} — {stats['translated']} line(s) in "
             f"{stats['calls']} request(s)")
        return True


class _Glosser:
    """Glossing runs BESIDE the rip, not after it.

    The two halves of an episode share nothing: ripping is Chrome and Language Reactor, and
    glossing is `claude -p` against a Primary already on disk. Run in sequence they idled in
    turn — measured on The Eminence in Shadow, the browser sat still for the ~4 minutes of six
    Opus requests, and the glosser sat still through the next episode's ASR wait — so a season
    cost the sum of both where it need only cost the larger.

    One worker, not a pool. The rip is one Chrome tab and cannot be parallel at all; the
    glosser *could* be, and deliberately isn't — `lib/claude.py` records that parallel calls
    just get rate-limited, and a queue that runs ahead of the rip would only pile up work whose
    input doesn't exist yet. So this is a pipeline of exactly two stages, and the season's
    wall-clock becomes the slower of the two rather than their sum.

    Every episode's lines are tagged with its number, since two jobs now narrate at once.
    Failures stay where they were: `_gloss` never raises, so a bad gloss lands in
    ``_UNGLOSSED`` for `gigaku translate` and the rip never learns about it.
    """

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._work, daemon=True, name="gigaku-gloss")
        self._started = False

    def submit(self, primary: Path, secondary: Path, tag: str, on_done=None) -> None:
        """Queue an episode. ``on_done`` runs only if the Secondary was actually written —
        it is how the extension's index learns the episode, and an index entry for a pair
        that doesn't exist would be a lie."""
        if not self._started:
            self._started = True
            self._thread.start()
        self._queue.put((primary, secondary, tag, on_done))

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            primary, secondary, tag, on_done = item
            try:
                if _gloss(primary, secondary, tag) and on_done:
                    on_done()
            except Exception as e:  # noqa: BLE001 — a worker that dies takes the rest with it
                _UNGLOSSED.append(primary.name)
                note(f"{tag} ✗ glosser failed: {e}")

    def pending(self) -> int:
        return self._queue.qsize()

    def finish(self, wait: bool = True) -> None:
        """Drain the queue (or walk away from it).

        ``wait=False`` is for a run that was stopped: the thread is a daemon, every answered
        request is already in the work file, and `gigaku translate` resumes from there — so
        Ctrl-C should not be made to sit through four more minutes of an episode nobody is
        waiting for.
        """
        if not self._started:
            return
        if not wait:
            left = self.pending()
            if left:
                print(f"  ({left} episode(s) left unglossed — finish with `gigaku translate`)")
            return
        if self.pending():
            print(f"\nRip done — waiting for the glosser ({self.pending()} episode(s) behind)…")
        self._queue.put(None)
        self._thread.join()


def _export_episode(ep: dict, audio: str, sub_name: str, sub_is_asr: bool,
                    series: str, season: int, title_dir: Path, attempts: int = 3,
                    glosser: "_Glosser | None" = None) -> str:
    """Run one episode end to end, re-checking every step: load+verify tracks → (translations)
    → re-verify → export xlsx → verify file → convert to Primary SRT → delete the xlsx →
    gloss the Secondary. On any failure, reload and retry. Returns the Primary .srt filename.

    **The Secondary comes from `lib/subs/translate.py` unless SUBS_TRANSLATE is off.** With it
    on, LR is never asked for machine translations at all: `_wait_translations` — thousands of
    line-by-line fetches, LR's rate limit, and the revert-mid-wait failure `_TrackWatch` exists
    to catch — is skipped outright, which is the longest and least reliable stretch of a rip.
    """
    glossing = settings.SUBS_TRANSLATE
    base = naming.srt_base(series, season, ep["seq"])
    primary, secondary = title_dir / f"{base} - Primary.srt", title_dir / f"{base} - Secondary.srt"
    for attempt in range(1, attempts + 1):
        try:
            aid, sid = _load_track(ep, audio, sub_name, sub_is_asr)
            _pause()
            # Watches the track through the long waits and re-selects the tick LR reverts it,
            # so a revert costs one click instead of a whole episode's work.
            watch = _TrackWatch(aid, sid, sub_name, sub_is_asr)
            if sub_is_asr:
                _wait_asr_complete(watch)   # "loaded" ≠ "finished" — see ASR_MIN_COVERAGE
            if not glossing:
                _wait_translations(watch)
            recheck = _verify_tracks(audio, sub_name, sub_is_asr)  # verify again before export
            if not recheck.get("ok"):
                # The waits above repair a revert as it happens, so reaching here means one
                # landed in the gap between the last tick and this check. Same cure, and the
                # checks the re-select invalidates are redone.
                print(f"  tracks drifted to {recheck.get('tmType')}:{recheck.get('tmName')}"
                      " before export — re-selecting")
                recheck = _reassert_tracks(aid, sid, audio, sub_name, sub_is_asr)
                if recheck.get("ok"):
                    watch = _TrackWatch(aid, sid, sub_name, sub_is_asr)
                    if sub_is_asr:
                        _wait_asr_complete(watch)
                    if not glossing:
                        _wait_translations(watch)
                    recheck = _verify_tracks(audio, sub_name, sub_is_asr)
            if not recheck.get("ok"):
                raise ExportError(
                    f"tracks changed before export (audio={recheck.get('audioName')} "
                    f"sub={recheck.get('tmType')}:{recheck.get('tmName')})")
            # The end of the programme, not of the transcription — see `creditsMs` above.
            # `min`, because an episode whose dialogue stops before the credits must still
            # export every cue it has.
            _last, _credits = recheck.get("lastMs"), recheck.get("creditsMs")
            expected_end_ms = (min(_last, _credits) if _last and _credits
                               else recheck.get("bodyMs") or _last)
            _seek_start()                          # rewind before exporting
            # The rewind is what makes LR rebuild, and a rebuild is when it reverts to the
            # title's default — the measured E11/E12 failure landed in exactly this gap, after
            # the verify above had already passed. Looking again here costs one call and
            # catches the common case; what settles it is the file's own text, below.
            after = _verify_tracks(audio, sub_name, sub_is_asr)
            if not after.get("ok"):
                raise ExportError(
                    f"tracks reverted over the rewind (sub={after.get('tmType')}:"
                    f"{after.get('tmName')}) — not exporting the wrong track")
            xlsx = Path(CHROME_DOWNLOAD_DIR) / _export(want_mt=not glossing)
            # `language=` is the check no label can fake: LR names the track it *meant* to
            # export, and E11/E12 proved that name survives a revert the text does not.
            ok, why = excel_to_srt.verify(xlsx, expected_end_ms,
                                          require_translation=not glossing,
                                          language=sub_name)
            if not ok:
                xlsx.unlink(missing_ok=True)       # drop bad file so we retry
                if why.startswith(excel_to_srt.WRONG_TRACK):
                    raise WrongTrack(f"exported file rejected — {why}")
                raise ExportError(f"exported file incomplete — {why}")
            title_dir.mkdir(parents=True, exist_ok=True)
            excel_to_srt.convert(xlsx, primary, None if glossing else secondary)
            if not (primary.exists() and primary.stat().st_size):
                raise ExportError("SRT conversion produced an empty/missing Primary")
            if not glossing and not (secondary.exists() and secondary.stat().st_size):
                raise ExportError("SRT conversion produced an empty/missing Secondary")
            xlsx.unlink()                          # delete the xlsx only after checks + conversion
            print(f"  ✓ {why}; wrote {primary.name}")
            _seek_start()                          # rewind again after exporting
            if glossing:
                # Outside the retry loop's concern: the browser work is done and the Primary
                # is on disk, so a failed gloss must not re-rip the episode. It is reported
                # and left for `gigaku translate`, which resumes from the lines already done.
                # And it is HANDED OFF rather than run here — the tab is free the moment the
                # Primary lands, so the next episode's rip starts now and this episode's
                # Russian arrives beside it (see ``_Glosser``).
                tag = f"E{ep['seq']:02d}"
                # The index goes with the gloss, not with the rip: an episode enters it only
                # as a complete pair, so registering it here would drop the Netflix id.
                index = lambda: _index(title_dir.parent, ep, series, season)  # noqa: E731
                if glosser is not None:
                    glosser.submit(primary, secondary, tag, on_done=index)
                elif _gloss(primary, secondary, tag):
                    index()
            return primary.name
        except _Aborted:
            raise
        except (ExportError, AppleScriptError) as e:
            if attempt >= attempts:
                raise
            print(f"  episode attempt {attempt} failed: {e} — retrying")
            _recover_episode(e)


# A tab on ``netflix.com/title/<id>`` — where clicking a show from a browse row lands you, and
# where `gigaku titles` sends you — is one navigation away from the player, so a run that
# refused there was refusing over a tab already on the right show. Netflix resolves
# ``/watch/<title id>`` to the episode you would resume, itself: measured, /watch/81159258
# turned into /watch/81205759 (episode 1 of Crash Landing on You) in under a second. That is
# the whole mechanism — Netflix's own answer, with no Play-button selector to break on a
# redesign or an interface language, and no click.
_TITLE_URL = re.compile(r"netflix\.com/title/(\d+)")

# The player has to exist before anything else can be checked, and a cold Netflix page is slow.
PLAYER_START_GIVEUP = 90


def _start_player() -> None:
    """Start the episode when the tab is on a show's title page rather than on the player.

    Does nothing in every other case: an actual watch tab needs no help, and a tab on /browse
    (or no Netflix tab at all) is ``_preflight_page``'s to explain, which it does far better
    than a guess from here would.
    """
    state, url = _tab_state()
    if state != "away":
        return
    found = _TITLE_URL.search(url)
    if not found:
        return

    global _WATCH_URL, _LAST_NAV
    _WATCH_URL = f"https://www.netflix.com/watch/{found.group(1)}"
    _LAST_NAV = time.time()
    print(f"Netflix is on the title page — starting the player ({url})")
    if _chrome_tab(f'set URL of t to "{_WATCH_URL}"', match=NETFLIX_MATCH) != "ok":
        raise ExportError(f"couldn't open the player from {url}")

    end = time.time() + PLAYER_START_GIVEUP
    while time.time() < end:
        time.sleep(2)
        try:
            # Polling with _run_main is also what keeps this silent: every call re-asserts
            # _NO_PLAY, so the episode Netflix just started is neutralised by the same loop
            # that waits for it — the run stays as invisible as it is for every other episode.
            # The <video> is part of the gate, not just the player session: measured, the
            # session answers ~2s before the element exists, and returning in that window
            # hands back a page where there is still nothing for ``_pause`` to pause.
            if _run_main(_PLAYER + "o.ready=!!(p&&p.getMovieId()&&"
                         "document.querySelector('video'));").get("ready"):
                _pause()
                return
        except AppleScriptError:
            pass  # the watch page isn't answering yet — it is still being built
    raise ExportError(f"the player didn't come up at {_WATCH_URL} — open the episode and re-run")


# Preflight is in two halves, and the split is what puts the banner before the waiting.
# Everything the *page* has to offer — Chrome, the tab, the Netflix globals, the player — can
# be checked instantly, and Netflix alone already knows the whole season (see
# ``_season_episodes``). Language Reactor is the slow half: a cold toggle can sit at LOADING
# for minutes. So the page is checked, the season is announced, and only then is LR switched
# on and asked about tracks — instead of making the user watch "turning it on…" with no idea
# which episodes are about to be downloaded.

def _require_one_netflix_tab() -> None:
    """Refuse to start while Chrome has more than one Netflix tab open.

    Every step of a run reaches its tab by URL substring and takes the **first** match — that
    is what lets the whole thing happen in the background, with no focus and no window in
    front. With a second Netflix tab open "the Netflix tab" stops naming one tab, and nothing
    downstream can recover the difference: ``_njs``/``_chrome_tab`` match ``netflix.com/watch``
    while ``_tab_state``/``_start_player``/``_recover_tab`` match the wider ``netflix.com``, so
    the halves of one run can measure one tab and steer another. Chrome's own order isn't
    stable either — moving a tab between windows reorders the walk mid-run.

    What the wrong tab costs is not a failed export: the run pauses the video, makes ``play()``
    a no-op, takes over Language Reactor's track and switches LR off when it ends. So this is a
    refusal before anything is touched, not a warning — including before ``_start_player``,
    which navigates a tab by the wide match and would otherwise be the first thing to grab the
    wrong one.
    """
    try:
        tabs = _netflix_tabs()
    except AppleScriptError:
        return  # Chrome isn't answering — _preflight_page diagnoses that far better than this
    if len(tabs) < 2:
        return
    listed = "\n".join(f"  {url}" for url in tabs)
    raise ExportError(
        f"{len(tabs)} Netflix tabs are open in Chrome — a run drives whichever one Chrome "
        "hands over first, so leave only the show to rip open, close the others, then "
        f"re-run:\n{listed}"
    )


def _preflight_page() -> None:
    """The instant half: Chrome, the tab, Netflix, the LR extension, the player.

    Raises ``ExportError`` with a specific, actionable message — failures surface immediately,
    not deep into a long run. Nothing here waits on Language Reactor.
    """
    # 1. Chrome running, a Netflix /watch tab open, JS-from-Apple-Events on.
    try:
        _njs("1")
    except AppleScriptError as e:
        msg = str(e)
        if "turned off" in msg:
            raise ExportError(_JS_OFF) from e
        if "not found" in msg:
            raise ExportError(
                "No Netflix show open in Chrome — open one at netflix.com/title/… or "
                "netflix.com/watch/… , then re-run."
            ) from e
        if getattr(e, "error_number", None) == -600 or "isn’t running" in msg or "not running" in msg:
            raise ExportError("Google Chrome isn't running — open it with a Netflix episode.") from e
        raise ExportError(f"couldn't reach the Netflix tab: {msg}") from e

    # 2. Netflix page + the Language Reactor extension + the player — the basics LR needs
    #    before it can load anything. Checked before turning LR on so failures stay specific.
    base = _run_main(
        "o.hasNetflix=(typeof netflix!=='undefined');o.hasLln=(typeof window.lln!=='undefined');"
        "o.playerReady=false;"
        "try{" + _PLAYER + "o.playerReady=!!(p&&p.getMovieId());}catch(e){}"
    )
    if not base.get("hasNetflix"):
        raise ExportError("This Chrome tab isn't a Netflix watch page.")
    if not base.get("hasLln"):
        raise ExportError("Language Reactor isn't active on this tab — install/enable the "
                          "Language Reactor extension and reload the Netflix page.")
    if not base.get("playerReady"):
        raise ExportError("The Netflix player isn't ready — open and play the actual episode "
                          "(not the title/preview screen), then re-run.")


def _preflight_tracks(audio: str, sub_name: str, sub_is_asr: bool) -> None:
    """The slow half: switch Language Reactor on, then check Pro and the requested tracks.

    Called *after* the run has announced the episodes it is going to download, because this is
    the step that takes minutes.
    """
    # 3. If the user has LR toggled off it never loads the tracks — turn it on and wait for
    #    the metadata to arrive before checking Pro / track availability below.
    _ensure_lln_on()

    # 4. Pro + the requested audio/subtitle tracks are actually available — one main-world probe.
    want = "asr" if sub_is_asr else "subtitles"
    d = _run_main(
        f"var A={json.dumps(audio)};var S={json.dumps(sub_name)};var T={json.dumps(want)};"
        "function ci(s){return (''+(s||'')).toLowerCase();}"
        "o.pro=null;o.title=null;o.mmReady=false;o.signedIn=null;o.license=null;"
        "o.audioAvail=[];o.subAvail=[];o.audioOk=false;o.subOk=false;"
        "try{var u=window.lln&&lln.userStore_extShared;o.pro=!!(u&&u.proFeaturesEnabled);"
        "o.signedIn=!!(u&&u.auth&&u.auth.haveAuth);"
        "o.license=(u&&u.data&&u.data.licenseStatus)||null;}catch(e){}"
        "try{var mm=lln.subManager.data.mm;o.mmReady=!!(mm&&mm.audioTracks&&mm.textTracks);"
        "var t=mm.titles;var k=Object.keys(t)[0];o.title=(t[k]&&t[k][0])||null;"
        "o.audioAvail=mm.audioTracks.map(function(x){return x.name;});"
        "o.subAvail=mm.textTracks.map(function(x){return x.type+':'+x.name;});"
        "o.audioOk=mm.audioTracks.some(function(x){return ci(x.name)===ci(A);});"
        "o.subOk=mm.textTracks.some(function(x){return x.type===T&&ci(x.name)===ci(S);});"
        "}catch(e){}"
    )
    # Signed out and un-subscribed are the same `proFeaturesEnabled: false` and two different
    # fixes, so the store is read rather than the flag alone. Measured 2026-08-20: a signed-out
    # LR reads {auth.haveAuth: false, data.licenseStatus: "NOT_SIGNED_IN"} — and this refused a
    # whole season telling a paying user to subscribe, which cost the run and the time it took
    # to find that the subscription was never the problem. `licenseStatus` cannot decide it on
    # its own either: signed in with Pro live it reads **"ZOMBIE"** (measured the same evening,
    # `pro: true` beside it), so only the NOT_SIGNED_IN reading is ever acted on. One reading
    # is enough — measured on a cold LR load, `pro` answers true from the first probe, 1.0s in
    # and while `state` is still LOADING: the auth store fills independently of the subtitle
    # manager and never passes through a false phase, so there is nothing here to re-ask.
    # It stays scoped to the Pro half of the flow: LR signed out still exports the free
    # non-ASR tracks, and refusing those runs would refuse work that does succeed.
    if sub_is_asr and d.get("pro") is False:
        if d.get("signedIn") is False or d.get("license") == "NOT_SIGNED_IN":
            raise ExportError(
                "Language Reactor is signed out, so it cannot see your Pro subscription and "
                "ASR subtitles stay locked. Click the Language Reactor icon in Chrome, sign "
                f"in, then re-run. (LR licenseStatus: {d.get('license')})")
        raise ExportError("Language Reactor Pro is required for ASR subtitles and machine "
                          f"translations (LR licenseStatus: {d.get('license')}). Subscribe, "
                          "or pass a non-ASR subtitle track.")
    if not d.get("mmReady"):
        raise ExportError("Language Reactor hasn't loaded this episode's tracks yet — make "
                          "sure the episode is actually playing, then re-run.")
    title = d.get("title") or "this title"
    if not d.get("audioOk"):
        raise ExportError(f"No '{audio}' audio for {title}. Available audio: {d.get('audioAvail')}.")
    if not d.get("subOk"):
        kind = "ASR " if sub_is_asr else ""
        raise ExportError(f"No '{sub_name}' {kind}subtitles for {title}. "
                          f"Available subtitles: {d.get('subAvail')}.")


def run(audio: str = "German", subtitle: str = "ASR Pro German",
        episodes: "episode_range.Range | None" = None) -> None:
    # A parsed Range, not a string: parsing in lib/cli.py is what keeps a malformed range
    # from importing this module (and pyobjc) at all.
    sub_name, sub_is_asr = _parse_subtitle(subtitle)
    _require_one_netflix_tab()  # first: everything below reaches its tab by "the first match"
    _start_player()  # a title page is one navigation from the player — open it, don't refuse
    _preflight_page()  # fail fast with a clear message; the LR half waits until after the banner

    _PAUSE_KEEP.set()
    threading.Thread(target=_pause_keeper, daemon=True).start()

    with DisplayWakeLock("gigaku subs"):  # keep the display awake so playback keeps buffering
        _check_tab()

        all_eps, cur, series, season = _season_episodes()
        # Where the tab belongs before the first _goto — so a Netflix bounce during episode
        # one can still be steered back instead of ending the run.
        global _WATCH_URL
        _WATCH_URL = f"https://www.netflix.com/watch/{cur}"
        # The library, not the download folder: SRT_TARGET_DIR is the chrome/gigaku
        # extension's own subs/ folder, so an episode is loadable by Migaku the moment it lands.
        subs_root = Path(settings.SRT_TARGET_DIR or CHROME_DOWNLOAD_DIR)
        title_dir = subs_root / naming.sanitize(series)

        done: list[str] = []
        failed: list[str] = []
        stopped = False
        # The second stage of the pipeline. Started lazily on its first episode, so a run with
        # nothing to fetch (or with SUBS_TRANSLATE off) never creates a thread at all.
        glosser = _Glosser() if settings.SUBS_TRANSLATE else None
        orig_ap = None  # read once LR is on, below — reading it earlier just answers None
        # The range's *bounds* are checked from here on, not in lib/cli.py: whether this
        # season has an episode 12 isn't knowable until Netflix has answered. Inside the
        # try/finally, so a rejected range still leaves the pause-keeper cleared and LR as it
        # found it — but NOT above _season_episodes(), whose _Aborted would then fall through
        # to the title_dir print below with the name unbound.
        try:
            if episodes is not None and season is None:
                raise UserError("this is a film, not a season — there is one export and no "
                                f"episodes to range over, so re-run without {episodes.text!r}")
            eps = episode_range.select(all_eps, episodes, f"{series} season {season}")
            # Resume is resolved *before* the banner, not inside the loop, so the banner can
            # name the episodes this run will actually download. On a resumed season most of
            # the requested range is already on disk, and announcing the requested span would
            # promise episodes nothing is going to fetch.
            todo = [e for e in eps if not _primary_srt(title_dir, series, season, e).exists()]
            headline, rows = episode_range.describe(todo, eps, len(all_eps), season, episodes)
            # Two blocks, not one list: which episodes is the question the run is being asked,
            # and the tracks and the folder are how it will answer — burying the first in the
            # second is what a single run of six rows did. One label width keeps them a table.
            tech = [("Audio", audio), ("Subtitle", subtitle), ("Into", str(title_dir))]
            label = max(len(name) for name, _ in rows + tech)

            def block(pairs):
                return "\n".join(f"  {name:<{label}}  {value}" for name, value in pairs)

            print(
                f"\n{series} — {headline}\n\n{block(rows)}\n\n{block(tech)}"
                + "\n\nRunning in the background — the Netflix tab does NOT need focus, so\n"
                "keep using your computer. To stop: press Ctrl-C here, or close the tab."
            )
            # Only now the slow half: switching Language Reactor on and waiting for its tracks
            # can take minutes, and it used to run *before* any of the above — so the user
            # watched it wake up with no idea which episodes it was waking up for. A run with
            # nothing to fetch now skips waking it at all.
            if todo:
                _preflight_tracks(audio, sub_name, sub_is_asr)
                orig_ap = _get_autopause()  # LR is on now, so its setting can be read + restored
            for ep in todo:
                print(f"\n=== E{ep['seq']:02d}  {ep['title']}  (id {ep['id']}) ===")
                try:
                    # Inside the guard, not above it: since a silent Chrome is now a
                    # *recoverable* error rather than a stop, it must land where the other
                    # recoverable ones do — one skipped episode, not a season ending on an
                    # exception nothing here catches.
                    _check_tab()
                    name = _export_episode(ep, audio, sub_name, sub_is_asr,
                                           series, season, title_dir, glosser=glosser)
                    done.append(name)
                    # Record the Netflix id → episode mapping the extension matches on. Doing
                    # it per episode (not once at the end) means a Ctrl-C'd season still leaves
                    # every finished episode loadable. On the glossing path the pair isn't
                    # complete yet, so this refreshes the index for what IS complete and the
                    # id lands from `_index` when the Secondary arrives; with --no-translate
                    # both tracks are already on disk and this is the only registration.
                    _index(subs_root, ep, series, season)
                except (ExportError, AppleScriptError) as e:
                    # One episode giving up must not kill the whole season — log it and move
                    # on; a re-run resumes and retries the ones still missing a Primary.srt.
                    print(f"  ✗ E{ep['seq']:02d} failed after retries: {e} — skipping to next")
                    failed.append(f"E{ep['seq']:02d}")
        except _Aborted as e:
            stopped = True
            print(f"\nStopped: {e.reason}. Re-run to resume at the next episode.")
        except KeyboardInterrupt:
            stopped = True
            print("\nStopped (Ctrl-C). Re-run to resume at the next episode.")
        finally:
            _PAUSE_KEEP.clear()
            try:
                if orig_ap is not None:
                    _set_autopause(orig_ap)  # restore the user's LR auto-pause setting
                _toggle_lln(False)           # leave LR off when the run ends (per request)
            except (AppleScriptError, ExportError):
                pass  # the tab may be gone by now — nothing to restore into

    # Outside the wake lock and after LR has been switched off: the browser's part is over, so
    # the last episodes' Russian arrives with the tab already handed back to the user. A run
    # that was stopped doesn't wait — see ``_Glosser.finish``.
    if glosser is not None:
        glosser.finish(wait=not stopped)

    print(f"\nWrote {len(done)} episode(s) this run → {title_dir}")
    for name in done:
        print(f"  {name}")
    if failed:
        print(f"Failed (re-run to retry): {', '.join(failed)}")
    if _UNGLOSSED:
        # Named separately from `failed` because the cure is different: these episodes have
        # their German and need no browser, so re-running the rip would skip them forever.
        print(f"No Secondary yet ({len(_UNGLOSSED)}): {', '.join(_UNGLOSSED)}")
        print("  Finish them with: gigaku translate")


if __name__ == "__main__":
    _audio = sys.argv[1] if len(sys.argv) > 1 else "German"
    _subtitle = sys.argv[2] if len(sys.argv) > 2 else "ASR Pro German"
    _range = episode_range.parse(sys.argv[3]) if len(sys.argv) > 3 else None
    try:
        run(_audio, _subtitle, episodes=_range)
    except ExportError as exc:
        print(f"subs failed: {exc}")
        raise SystemExit(1)
    except _Aborted as exc:
        print(f"stopped before the run could start: {exc.reason}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("stopped (Ctrl-C) before the run could start.")
        raise SystemExit(1)
