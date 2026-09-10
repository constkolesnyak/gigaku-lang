#!/usr/bin/env python3
"""Import freqdeck/out/frequency_de.apkg through MvJ's OWN importer, headlessly.

Run inside Anki via the one-shot add-on:

    echo $PWD/freqdeck/anki_import.py > /tmp/gigaku_oneshot_script.txt

and read /tmp/gigaku_freq_import.json from a shell. Same call as the one
`ui._show_import_deck_dialog` makes for a German apkg: target_notetype_id + preserve_guids=True, so a re-run UPDATES the notes
whose guid (the word) is already there and an empty source field leaves the live value
(definitions, audio, am-*) alone. [sound:] becomes [audio:] on the way in.
"""
import importlib
import json
import os
import traceback

# Which apkg to import: /tmp/gigaku_import.txt if it names one (deck.py writes one file
# per source list), else the general-frequency list's.
ARGS = "/tmp/gigaku_import.txt"
APKG = (open(ARGS).read().strip() if os.path.exists(ARGS)
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "out",
                          "frequency_de-words.apkg"))
DECK = "Frequency 🇩🇪"
NOTETYPE = "🇩🇪 German"
RESULT = "/tmp/gigaku_freq_import.json"

out = {"ok": False, "apkg": APKG, "deck": DECK}
try:
    from aqt import mw

    importer = importlib.import_module("MvJ Japanese.mvj.importer")
    actions = importlib.import_module("MvJ Japanese.mvj.actions")

    model = mw.col.models.by_name(NOTETYPE)
    assert model, f"{NOTETYPE} not in this collection"
    out["notetype_id"] = model["id"]
    target_names = [f["name"] for f in model["flds"]]
    out["notes_before"] = mw.col.db.scalar("SELECT count(*) FROM notes WHERE mid = ?", model["id"])

    deck = importer.extract_apkg(APKG)
    assert deck, "extract_apkg returned nothing"
    out["source_notes"] = len(deck.notes)
    out["source_fields"] = [f.name for f in deck.source_fields]

    cfgm = actions.get_config_manager()
    mapping = importer.auto_map_fields(deck.source_fields, deck.notes, cfgm,
                                       target_field_names=target_names)
    by_name = {f.name: f.ordinal for f in deck.source_fields}
    for name in target_names:
        if name in by_name:
            mapping[name] = by_name[name]

    count, error = importer.import_deck(deck, mapping, DECK, cfgm,
                                        target_notetype_id=model["id"],
                                        preserve_guids=True)
    out["imported"] = count
    out["error"] = error
    out["notes_after"] = mw.col.db.scalar("SELECT count(*) FROM notes WHERE mid = ?", model["id"])
    out["deck_cards"] = mw.col.db.scalar(
        "SELECT count(*) FROM cards WHERE did = (SELECT id FROM decks WHERE name = ?)", DECK)
    out["ok"] = not error
except Exception:
    out["traceback"] = traceback.format_exc()

with open(RESULT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
