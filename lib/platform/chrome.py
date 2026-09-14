"""Drive a Chrome tab from Python: JavaScript, navigation, and adaptive waits.

Chrome runs `execute javascript` in an *isolated* world — it shares the DOM but not page
globals (`window.netflix`, `window.lln`). Reaching a page global therefore needs a second
hop: inject a `<script>` that computes a result and writes it into a hidden DOM node, then
read that node back from the isolated world. ``Page.main()`` is that hop.

Two domains drive Chrome through this module: lib/subs/subs.py (the Language Reactor subtitle
exporter, which predates the ``Page`` class and still keeps its own thin wrappers over the
same primitives) and lib/netflix/gallery.py (the browse-gallery rip). What lives here is only
what neither of them can own alone — the visibility spoof, the injection wrapper, the
navigation barrier, and the stall-detecting wait.
"""
import json
import threading
import time

from lib.platform import applescript
from lib.platform.applescript import AppleScriptError

JS_OFF = (
    "Chrome is blocking JavaScript from Apple Events. Enable it once: "
    "Chrome ▸ View ▸ Developer ▸ 'Allow JavaScript from Apple Events'."
)

# Serialize every AppleScript call: NSAppleScript isn't safe under concurrent calls, and a
# driver may run background threads that touch the browser at the same time as the main flow.
# An RLock, so a caller holding its own lock around a group of calls still nests fine.
_LOCK = threading.RLock()

# Seconds with NO forward progress before ``wait_progress`` calls it a stall. Waits here are
# adaptive rather than fixed timeouts: they ride out a slow server for as long as it keeps
# making progress, and give up only when it stops making any.
STALL_GIVEUP = 120

# Make the page permanently believe it is foreground. Netflix tears the player down / won't
# switch or generate ASR subtitles in a hidden tab; overriding the Page Visibility API in the
# MAIN world (and swallowing visibilitychange) lets a whole run happen while the tab sits in
# the background — so the user can switch tabs/apps and keep working. Prepended to every
# ``Page.main`` body so it is (re)asserted on every call, including right after a page reload
# (which wipes it). Idempotent; self-contained; never touches ``o``.
SPOOF_VIS = (
    "try{Object.defineProperty(document,'visibilityState',{configurable:true,get:function(){return 'visible';}});"
    "Object.defineProperty(document,'hidden',{configurable:true,get:function(){return false;}});"
    "Object.defineProperty(document,'webkitVisibilityState',{configurable:true,get:function(){return 'visible';}});"
    "Object.defineProperty(document,'webkitHidden',{configurable:true,get:function(){return false;}});"
    "document.hasFocus=function(){return true;};"
    "if(!window.__gigaku_vis){window.__gigaku_vis=1;"
    "document.addEventListener('visibilitychange',function(ev){ev.stopImmediatePropagation();},true);"
    "document.addEventListener('webkitvisibilitychange',function(ev){ev.stopImmediatePropagation();},true);}"
    "}catch(_e){}"
)


class PageError(Exception):
    """A step failed in a way a retry might fix (Chrome busy, page mid-navigation)."""


class TabClosed(Exception):
    """The tab this Page targets is gone — stop, don't retry."""


# A JavaScript dialog — alert/confirm/prompt — on the target tab does not slow `execute
# javascript` down, it stops it: while one is up Chrome answers no JS on that tab at all.
# Measured on a wedged Netflix tab: a *non-JS* query about the same tab (its URL, its title,
# whether it is loading) came back in 0.1s, while `1+1` took **120.2s** to come back as error
# -1712 — AppleScript's default, and far too long to be useful, because ``applescript.run``
# then retries -1712 as a transient hiccup and every JS call costs six fruitless minutes. Every
# wait, every probe and every recovery path in these flows is a JS call, so a dialog leaves a
# run crawling and silent, which is indistinguishable from hung and is what it was reported as.
# An explicit `with timeout` is the only thing that shortens it (measured: 8.2s in, same -1712).
# A real call returns in milliseconds, so this is a wedge detector, not a deadline.
JS_TIMEOUT = 30


def exec_js_on_extension(url_substring: str, js: str) -> str | None:
    """Run ``js`` on the first Chrome tab whose URL contains ``url_substring``.

    Raises ``AppleScriptError`` "…not found" if no matching tab is open, and -1712 ("AppleEvent
    timed out") if the tab's renderer is blocked — see ``JS_TIMEOUT``. JS strings are escaped
    for embedding in the AppleScript literal, so use single quotes inside ``js``.
    """
    escaped = js.replace("\\", "\\\\").replace('"', '\\"')
    return applescript.run(f'''
with timeout of {JS_TIMEOUT} seconds
tell application "Google Chrome"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains "{url_substring}" then
                return execute t javascript "{escaped}"
            end if
        end repeat
    end repeat
    error "Tab with URL containing {url_substring} not found in Chrome"
end tell
end timeout
''')


def open_tab(url: str) -> None:
    """Open ``url`` in a new tab of the front Chrome window (creating a window if needed)."""
    with _LOCK:
        applescript.run(
            'tell application "Google Chrome"\n'
            "  if (count of windows) = 0 then make new window\n"
            f'  make new tab at end of tabs of front window with properties {{URL:"{url}"}}\n'
            "end tell"
        )


def wait_progress(measure, done, progress, desc: str, *, on_tick=None, on_stall=None,
                  log=None, interval: float = 2.0, giveup: float | None = None,
                  abort=None, error=PageError) -> dict:
    """Adaptive wait — no fixed deadline.

    Blocks as long as ``progress(result)`` keeps changing, so it rides out a slow server and
    returns the instant ``done(result)`` is true. ``on_tick`` runs before every measurement;
    ``on_stall(result)`` runs on ticks with no progress, and returning True from it means it
    *deliberately* waited (backing off a rate limit, say) — a handled pause, not a stall, so
    the clock restarts. Raises ``error`` only after ``giveup`` seconds with no change at all.

    ``abort`` is polled each tick and raises ``TabClosed`` when it returns True; ``error``
    lets a caller keep its own exception type (lib/subs/subs.py raises ``ExportError``).
    """
    last_key: object = wait_progress  # unique sentinel — the first tick always "changed"
    last_change = time.time()
    last_log = 0.0
    d: dict = {}
    while True:
        if abort and abort():
            raise TabClosed()
        if on_tick:
            try:
                on_tick()
            except AppleScriptError:
                pass
        try:
            d = measure()
        except AppleScriptError as e:
            if "turned off" in str(e):
                raise error(JS_OFF) from e
            d = {}  # tab momentarily gone mid-navigation
        if done(d):
            return d
        now = time.time()
        key = progress(d)
        if key != last_key:
            last_key, last_change = key, now
        else:
            handled = False
            if on_stall:
                try:
                    handled = bool(on_stall(d))
                except AppleScriptError:
                    pass
            if handled:
                last_change = time.time()
            else:
                limit = STALL_GIVEUP if giveup is None else giveup
                if now - last_change > limit:
                    raise error(f"{desc}: no progress for {limit:g}s (last={d})")
        if log and now - last_log > 4:
            last_log = now
            log(d)
        time.sleep(interval)


class Page:
    """One Chrome tab, found by URL substring — never by "the active tab".

    Matching by URL is what lets a run happen entirely in the background: the tab needs
    neither focus nor foreground, so the user keeps working while it drives. ``prelude`` is
    JS prepended to every ``main()`` body (``SPOOF_VIS`` by default) — a reload wipes
    page-level patches, so they have to be re-asserted rather than installed once.
    """

    def __init__(self, url_match: str, *, prelude=(SPOOF_VIS,), node: str = "__gigaku_x__",
                 error=PageError):
        self.url_match = url_match
        self.prelude = "".join(prelude)
        self.node = node
        self.error = error

    # ── JavaScript ───────────────────────────────────────────────────────────

    def dom(self, js: str) -> str | None:
        """Run JS in the tab's isolated world (DOM only — no page globals)."""
        try:
            with _LOCK:
                return exec_js_on_extension(self.url_match, js)
        except AppleScriptError as e:
            if "turned off" in str(e):
                raise self.error(JS_OFF) from e
            raise

    def main(self, body: str) -> dict:
        """Run ``body`` in the page's MAIN world and return its ``o`` object as a dict.

        ``body`` must assign its results onto the pre-declared ``o`` object. Never raises on
        in-page errors, because every caller polls rather than trapping: a thrown body yields
        ``{"__err": ...}``, a page mid-navigation (globals not ready) yields ``{}``, and
        output that isn't JSON yields ``{"__raw": ...}``.
        """
        payload = (
            "(function(){var el=document.getElementById('" + self.node + "');var o={};"
            "try{" + self.prelude + body + "}catch(e){o.__err=''+((e&&e.message)||e);}"
            "el.textContent=JSON.stringify(o);})();"
        )
        wrapper = (
            "var d=document.getElementById('" + self.node + "');"
            "if(!d){d=document.createElement('div');d.id='" + self.node + "';"
            "d.style.display='none';document.documentElement.appendChild(d);}"
            "d.textContent='';"
            "var s=document.createElement('script');s.textContent=" + json.dumps(payload) + ";"
            "document.documentElement.appendChild(s);s.remove();d.textContent"
        )
        raw = self.dom(wrapper)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {"__raw": raw}

    # ── Tab / navigation ─────────────────────────────────────────────────────

    def tab(self, action: str) -> str:
        """Run ``action`` (an AppleScript statement using ``t`` = the tab) on the matching
        tab, wherever it lives. Returns "ok", or "none" if no such tab is open."""
        with _LOCK:
            return applescript.run(
                'tell application "Google Chrome"\n'
                "  repeat with w in windows\n"
                "    repeat with t in tabs of w\n"
                f'      if URL of t contains "{self.url_match}" then\n'
                f"        {action}\n"
                '        return "ok"\n'
                "      end if\n"
                "    end repeat\n"
                "  end repeat\n"
                '  return "none"\n'
                "end tell"
            )

    def require(self) -> None:
        """Raise ``TabClosed`` if the tab is no longer open."""
        if self.tab("") != "ok":
            raise TabClosed()

    def closed(self) -> bool:
        """True only when the tab is gone. A *hidden* tab is not closed — visibility is
        spoofed, so a backgrounded tab keeps working and must not stop the run."""
        try:
            self.dom("1")  # cheapest probe — does a matching tab still exist?
        except AppleScriptError as e:
            if "turned off" in str(e):
                raise self.error(JS_OFF) from e
            if "not found" in str(e):
                return True   # tab closed → stop
            return False      # transient (Chrome busy / mid-navigation) — not a stop
        return False

    def url(self) -> str:
        cur = self.tab("return URL of t")
        if cur == "none":
            raise TabClosed()
        return cur or ""

    def navigate(self, action: str) -> None:
        """Run a navigating AppleScript statement and wait for the new document to be live.

        Navigating is asynchronous: `set URL`/`reload` return the moment Chrome accepts them
        while the OLD document keeps answering JS for a second or two. That document is fully
        loaded, so a state probe right after navigating succeeds against a page about to be
        thrown away — and the *next* call lands on the new blank document and dies. So: stamp
        the current document, navigate, and block until an *unstamped* one answers.
        """
        # Read the stamp back: if it didn't take (page mid-navigation, JS momentarily
        # unreachable) then "unstamped" is true from the start and the wait below would return
        # instantly — silently restoring the very race it exists to prevent.
        stamped = self.main(
            "window.__gigaku_page=1;o.k=1;o.stamped=!!window.__gigaku_page;"
        ).get("stamped")
        if self.tab(action) != "ok":
            raise TabClosed()
        if not stamped:
            time.sleep(3)
        wait_progress(
            lambda: self.main("o.alive=1;o.old=!!window.__gigaku_page;o.ready=document.readyState;"),
            lambda d: bool(d.get("alive")) and not d.get("old")
            and d.get("ready") in ("interactive", "complete"),
            lambda d: (d.get("alive"), d.get("old"), d.get("ready")),
            "page navigation", interval=0.5, giveup=40,
            abort=self.closed, error=self.error,
        )

    def goto(self, url: str) -> None:
        """Navigate the tab to ``url``. Already there → reload, so the document turns over
        either way and ``navigate`` can't wait forever for a navigation that never happens."""
        cur = self.url()
        self.navigate("reload t" if cur and cur.rstrip("/") == url.rstrip("/")
                      else f'set URL of t to "{url}"')

    def reload(self) -> None:
        self.navigate("reload t")
