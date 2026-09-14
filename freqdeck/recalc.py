#!/usr/bin/env python3
"""AnkiMorphs recalc (the R key) from inside Anki — run through oneshot_addon:

    echo $PWD/freqdeck/recalc.py > /tmp/gigaku_oneshot_script.txt

It hands off to a background op and returns at once; poll the notes' am-all-morphs-count
over AnkiConnect to see it finish (measured: ~45 s when little changed, ~4 min after a
big import over this 500k-note collection). Result JSON: /tmp/gigaku_recalc.json.
"""
import json
import sys

out = {}
try:
    from aqt import mw
    # the reviewer must not be holding a card while the collection is rebuilt
    try:
        mw.moveToState("deckBrowser")
    except Exception:
        pass
    sys.modules["472573498"].recalc.recalc_main.recalc()   # AnkiMorphs' add-on id
    out["ok"] = True
except Exception:
    import traceback
    out["traceback"] = traceback.format_exc()
json.dump(out, open("/tmp/gigaku_recalc.json", "w"))
