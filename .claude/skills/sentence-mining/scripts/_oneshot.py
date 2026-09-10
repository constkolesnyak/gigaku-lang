"""Run a script inside Anki's own process, on its main thread.

AnkiConnect cannot do this, and two things this skill needs can only be done there:
AnkiMorphs' recalc, and `reposition_new_cards` + `col.set_config`. `freqdeck/oneshot_addon`
can — but Anki loads add-ons only at startup, and the runner is meant to be installed for one
job and removed. So the sequence is: install, quit, relaunch, drop the trigger, wait, remove.

Anki is left running and exactly as it was, minus the runner.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GIGAKU = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
ADDONS = os.path.expanduser("~/Library/Application Support/Anki2/addons21")
RUNNER = os.path.join(ADDONS, "zz_gigaku_oneshot")
TRIGGER = "/tmp/gigaku_oneshot_script.txt"
LOG = "/tmp/gigaku_oneshot.log"


def note(m):
    print(m, file=sys.stderr, flush=True)


def anki_running():
    out = subprocess.run(["/bin/ps", "-ax", "-o", "command="],
                         capture_output=True, text=True).stdout
    return "Anki.app/Contents/MacOS/Anki" in out


def restart_anki():
    """Quit and relaunch so the runner is loaded.

    The quit is asked for and then simply waited out: AppleScript reports
    `User canceled (-128)` on this collection even when the quit does proceed, so the process
    list is the only thing worth believing. A refusal that outlasts the wait is real — Anki
    holds the quit while a media sync is running — and is reported rather than forced.
    """
    note("  quitting Anki…")
    subprocess.run(["osascript", "-e", 'tell application "Anki" to quit'],
                   capture_output=True, text=True)
    for _ in range(60):
        if not anki_running():
            break
        time.sleep(2)
    else:
        sys.exit("Anki would not quit — a media sync or a modal dialog is holding it. "
                 "Wait for it to settle and run this again.")
    note("  relaunching…")
    r = subprocess.run(["bash", os.path.join(HERE, "ensure_anki.sh")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Anki did not come back: {r.stderr.strip()[:300]}")


def installed():
    return os.path.isdir(RUNNER)


def install():
    """Copy the runner in if it is missing. Left in place afterwards — see `run`."""
    if not installed():
        shutil.copytree(os.path.join(GIGAKU, "freqdeck", "oneshot_addon"), RUNNER)
        return True
    return False


def armed(timeout=14):
    """Is a loaded runner listening? Proven by it CONSUMING the trigger, not by the folder.

    The addon deletes the trigger before running the script, so the file disappearing is
    the only honest evidence that this Anki process has the addon loaded — the folder can
    be there while the running Anki was started before it was copied in.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(TRIGGER):
            return True
        time.sleep(1)
    return False


def run(script, result_path, clean=()):
    """Execute `script` inside Anki, restarting it only when there is no other way.

    Anki loads add-ons at startup, so the runner used to be copied in, Anki quit and
    relaunched, and the runner removed again — which meant a quit + a bouncing dock icon
    on EVERY recalc, reorder and probe. The runner now stays installed, so a second call
    just drops the trigger and a listening Anki picks it up within its 5 s tick. A restart
    happens only when the trigger is not consumed, i.e. this Anki predates the install.
    """
    for f in (result_path, LOG, *clean):
        try:
            os.remove(f)
        except OSError:
            pass
    fresh = install()
    with open(TRIGGER, "w", encoding="utf-8") as f:
        f.write(script)
    if not fresh and armed():
        return                      # already listening — leave Anki alone
    note("  the runner is not loaded in this Anki — restarting it once")
    restart_anki()
    with open(TRIGGER, "w", encoding="utf-8") as f:
        f.write(script)


def cleanup():
    """Only the trigger. The runner stays installed on purpose: removing it is what forced
    a restart next time. Remove the folder by hand if it should not linger."""
    try:
        os.remove(TRIGGER)
    except OSError:
        pass
