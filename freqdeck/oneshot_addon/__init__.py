"""Disposable one-shot runner for Anki — runs a dropped script inside Anki's process.

Install:  cp -r freqdeck/oneshot_addon "~/Library/Application Support/Anki2/addons21/zz_gigaku_oneshot"
          and restart Anki.  REMOVE THE FOLDER WHEN DONE.

Every 5 s after the profile opens, if /tmp/gigaku_oneshot_script.txt names a script, the
trigger is removed FIRST (never a loop) and the script is exec'd on the main thread —
that is what MvJ's importer and AnkiMorphs' recalc need, and what AnkiConnect cannot do.
Scripts write their own result JSON; this logs to /tmp/gigaku_oneshot.log.
"""
import os
import traceback

TRIGGER = "/tmp/gigaku_oneshot_script.txt"
LOG = "/tmp/gigaku_oneshot.log"


def _log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def _tick():
    if not os.path.exists(TRIGGER):
        return
    script = open(TRIGGER, encoding="utf-8").read().strip()
    os.remove(TRIGGER)
    _log(f"running {script}")
    try:
        exec(compile(open(script, encoding="utf-8").read(), script, "exec"),
             {"__name__": "__oneshot__", "__file__": script})
        _log(f"done {script}")
    except Exception:
        _log(traceback.format_exc())


def _arm():
    from aqt.qt import QTimer
    timer = QTimer()
    timer.setInterval(5000)
    timer.timeout.connect(_tick)
    timer.start()
    _arm.timer = timer  # keep a reference or Qt collects it
    _log("armed")


from aqt import gui_hooks  # noqa: E402

gui_hooks.profile_did_open.append(_arm)
