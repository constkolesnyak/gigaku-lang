#!/usr/bin/env python3
"""Run AnkiMorphs' recalc without a human, then drop what it says is already known.

    python3 scripts/recalc.py            # restart Anki, recalc, sweep, report
    python3 scripts/recalc.py --no-sweep # recalc only

This is the last step of a build, and it exists because the deck cannot judge itself. The
mining side decides "unknown" from Migaku alone; AnkiMorphs decides it from its own known
morphs, and on the first build the two disagreed about 8 cards out of 400 — words already
known that would have been taught anyway. It also writes the real i-level
(`_card-status::i+0/i+1/i+≥2`), which is the tag worth studying by.

**Why it restarts Anki.** AnkiMorphs' recalc has to run on Anki's main thread; AnkiConnect
cannot do that, and `freqdeck/oneshot_addon` — which can — is loaded only at startup and is
meant to be installed for one job and removed. So: install, quit, relaunch, trigger, wait,
uninstall. Anki is left running and back the way it was, minus the runner.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import load_config, deck_main            # noqa: E402
from _anki import anki_request, quiesce                        # noqa: E402
import _oneshot                                      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GIGAKU = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
RESULT = "/tmp/gigaku_recalc.json"
KNOWN_TAG = "_card-status::i+0"   # AnkiMorphs' own; adding a second tag for it is clutter


def note(m):
    print(m, file=sys.stderr, flush=True)


def coverage(cfg):
    """(notes recalc has annotated in this deck, notes in the deck)."""
    d = deck_main(cfg)
    return (len(anki_request("findNotes", query=f'deck:"{d}" -am-all-morphs-count:')),
            len(anki_request("findNotes", query=f'deck:"{d}"')))


def wait_for_recalc(cfg, timeout):
    """Wait for the recalc to have run AND for the collection to stop moving.

    The obvious test — "am-all-morphs-count is filled on more notes than before" — only
    works the first time. On a rebuilt deck those fields are ALREADY filled, the count never
    rises, and the wait times out on a recalc that in fact succeeded: measured 2026-09-04,
    `{"ok": true}` in /tmp/gigaku_recalc.json while this script declared failure and skipped
    its own sweep and sync. So the run is proven by the runner's result file, and completion
    by the fingerprint holding still — recalc hands off to a background op, so returning is
    not finishing.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(RESULT):
            break
        time.sleep(5)
    else:
        sys.exit("the one-shot runner never ran — see /tmp/gigaku_oneshot.log")
    res = json.load(open(RESULT))
    if not res.get("ok"):
        sys.exit(f"recalc raised inside Anki: {res.get('traceback', res)}")

    # Wait for the deck to be COVERED, then for it to stop moving. Stability alone is not
    # enough: on a freshly built deck the reading starts at "nothing annotated", and a
    # fingerprint that never moves is indistinguishable from a recalc that has not begun —
    # measured, this returned after 45 s on a 139-card rebuild, found no verdict to sweep and
    # synced a deck AnkiMorphs had not yet seen. recalc() hands off to a background op, so
    # returning is not finishing.
    stable, last = 0, None
    while time.time() < deadline:
        time.sleep(15)
        try:
            done, total = coverage(cfg)
        except Exception:  # noqa: BLE001 — Anki is holding the collection; that IS the signal
            stable = 0
            continue
        covered = total and done >= total * 0.9
        stable = stable + 1 if (covered and (done, total) == last) else 0
        last = (done, total)
        if stable >= 2:
            break
    else:
        sys.exit(f"recalc never annotated this deck (covered {last}) — see /tmp/gigaku_oneshot.log")
    note(f"  annotated {last[0]}/{last[1]} of the deck")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sweep", action="store_true",
                    help="run the recalc but leave the already-known cards alone")
    ap.add_argument("--no-sync", action="store_true",
                    help="do not sync to AnkiWeb at the end. The deck is studied on a phone, "
                         "so an unsynced build has not actually been delivered")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    cfg = load_config()

    try:
        _oneshot.run(os.path.join(GIGAKU, "freqdeck", "recalc.py"), RESULT)
        note("  recalc triggered; waiting…")
        wait_for_recalc(cfg, a.timeout)
    finally:
        _oneshot.cleanup()

    q = f'deck:"{deck_main(cfg)}" tag:{KNOWN_TAG}'
    ids = anki_request("findNotes", query=q)
    words = [n["fields"]["Word"]["value"].strip()
             for n in (anki_request("notesInfo", notes=ids) if ids else [])]
    note(f"\n  AnkiMorphs calls {len(ids)} of the deck already known: {', '.join(words[:20])}")
    if ids and not a.no_sweep:
        # Suspend, and add no tag of our own: `_card-status::i+0` already says exactly this
        # and AnkiMorphs keeps it current. Nothing is deleted.
        anki_request("suspend", cards=anki_request("findCards", query=q))
        note(f"  suspended (they keep {KNOWN_TAG}); nothing deleted")
    live = len(anki_request("findCards", query=f'deck:"{deck_main(cfg)}" -is:suspended'))
    clean = len(anki_request("findCards", query=f'deck:"{deck_main(cfg)}" -is:suspended tag:_card-status::i+1'))
    note(f"\n  studying {live}, of which truly i+1: {clean}")

    if not a.no_sync:
        # A build that is not synced does not exist: the studying happens on the phone.
        # This is the last step for that reason — one sync at the end, not one per file.
        # Anki's own media sync then runs inside Anki, in the background, and a first build
        # is ~65 MB of clips, so it keeps going for a few minutes after this returns.
        # Move the reviewer away if the user is reviewing right now: a sync can bring a
        # deletion from the phone, and the reviewer holds the card object and throws
        # `NotFoundError: No such card` at the user on the next window focus change — and
        # repeats it on every focus change until the state is reset, even a day later.
        if quiesce():
            note("  reviewer moved to the deck list — the sync may remove the card under it")
        note("  syncing to AnkiWeb…")
        anki_request("sync")
        note("  synced (media continues in the background inside Anki)")


if __name__ == "__main__":
    main()
