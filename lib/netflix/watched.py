"""What you have already watched, from the **Netflix Watched Marker** Chrome extension.

The extension keeps its list in `chrome.storage.local` under `myNFWatchedList` — a flat array
of Netflix video ids. That storage is a LevelDB inside the Chrome profile and is reachable
only from the extension's own world: AppleScript's `execute javascript` runs in an *isolated*
world even on an extension page, where `chrome.storage` is undefined (measured). Reading the
LevelDB directly would mean a new dependency and a private on-disk format.

So this uses the extension's own supported export instead. Its popup has a **Backup Watched
List** button that writes one id per line to `~/Downloads/Netflix Watched List (<when>).txt`.
We open the popup, click that button, and read the file — the same trick `lib/subs/subs.py`
uses on Language Reactor's export button: a synthetic click from the isolated world still
fires the page's own handler, because both share the DOM.

Nothing here writes to the extension. Marking something watched stays the extension's job.
"""
import glob
import os
import time

from lib.config import CHROME_DOWNLOAD_DIR, UserError, note, settings
from lib.netflix import store
from lib.platform.applescript import AppleScriptError
from lib.platform.chrome import Page, PageError, open_tab

EXT_ID = "dbnjnblicgfgambjhbffkgihmlfokefn"
POPUP_URL = f"chrome-extension://{EXT_ID}/popup_nf.html"
EXPORT_GLOB = "*Watched List*.txt"
BUTTON = "btnBackupWatchedList"


def cache_path():
    return os.path.join(os.path.expanduser(settings.TITLES_DIR), "watched.json")


def read_cache():
    data = store.read_json(cache_path()) or {}
    return {str(i) for i in data.get("ids", [])}


def _exports():
    return set(glob.glob(os.path.join(os.path.expanduser(CHROME_DOWNLOAD_DIR), EXPORT_GLOB)))


def _parse(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return {line.strip() for line in f if line.strip().isdigit()}


def refresh(timeout=20):
    """Click the extension's own Backup button and read the ids it writes. Returns a set.

    Best-effort by design: the watched list is a nice-to-have on top of the gallery, so a
    missing extension or a Chrome that won't cooperate degrades to the cached list rather
    than failing the command.
    """
    page = Page(EXT_ID, error=PageError)
    opened = False
    try:
        if page.tab("") != "ok":
            open_tab(POPUP_URL)
            opened = True
            for _ in range(20):
                time.sleep(0.25)
                if page.tab("") == "ok":
                    break
            else:
                note("Netflix Watched Marker isn't installed (or its popup won't open) — "
                     "using the cached watched list.")
                return read_cache()

        # Wait for the popup's own script to bind its handlers; a click before jQuery is
        # ready lands on a button that does nothing and looks exactly like a failed export.
        for _ in range(20):
            if page.dom(f"document.getElementById('{BUTTON}') ? '1' : ''"):
                break
            time.sleep(0.25)
        else:
            raise PageError("the extension popup never rendered its Backup button")

        before = _exports()
        page.dom(f"document.getElementById('{BUTTON}').click(); '1'")

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.4)
            fresh = _exports() - before
            if fresh:
                path = max(fresh, key=os.path.getmtime)
                ids = _parse(path)
                os.unlink(path)  # regenerable, and it would pile up in ~/Downloads
                store.write_json(cache_path(), {"fetched_at": time.time(),
                                                "ids": sorted(ids, key=int)})
                note(f"Watched list: {len(ids):,} titles marked in Netflix Watched Marker.")
                return ids
        raise PageError(
            "the extension's Backup button produced no file. If Chrome is set to "
            "'Ask where to save each file', turn that off or run with --no-watched."
        )
    except (PageError, AppleScriptError, OSError) as e:
        cached = read_cache()
        note(f"Could not refresh the watched list ({e}) — "
             f"using the cached {len(cached):,}.")
        return cached
    finally:
        if opened:
            try:
                page.tab("close t")
            except (AppleScriptError, PageError):
                pass


def apply(records, ids):
    """Mark every record, so `watched` is a real column rather than a page-only notion."""
    for rec in records:
        rec["watched"] = str(rec.get("nf_id")) in ids
    return sum(1 for rec in records if rec["watched"])


def main(refresh_list=True):
    """The ids to use this run — refreshed from Chrome, or the cache."""
    if not refresh_list:
        return read_cache()
    try:
        return refresh()
    except UserError:
        raise
    except Exception as e:  # noqa: BLE001 - never let a nice-to-have take out the command
        note(f"Watched-list sync failed ({e}) — continuing without it.")
        return read_cache()
