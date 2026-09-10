#!/usr/bin/env python3
"""Russian translation of each sentence into `Notes` — the back of the card.

    python3 scripts/translate.py            # every studied card whose Notes is empty
    python3 scripts/translate.py --redo     # re-translate even where Notes is filled

The prompt, the request shape and the reply parser are **freqdeck's own** (`deck.SYSTEM_RU`,
`deck._RU_LINE`, groups of `deck.TRANSLATE_GROUP`), not a second rubric invented here: the
back of a `YouTube 🇩🇪` card has to read exactly like the back of a `Frequency 🇩🇪` card, and
the way to guarantee that is to ask the same question with the same words. `freqdeck/audit.py`
checks `Notes` is Cyrillic on every note, which is the same contract.

Runs on studied cards only, like every other enrichment step — a suspended card is one
already judged not worth teaching, and translating it is waste.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "freqdeck"))
from _config import load_config, deck_main       # noqa: E402
from _anki import anki_request                   # noqa: E402
from lib.claude import ask, describe             # noqa: E402
import deck as freqdeck                          # noqa: E402

MODEL, EFFORT, ATTEMPTS = "opus", "medium", 3


def note(m):
    print(m, file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--group", type=int, default=freqdeck.TRANSLATE_GROUP)
    a = ap.parse_args()
    cfg = load_config()
    d = deck_main(cfg)

    q = f'deck:"{d}" -is:suspended' + ("" if a.redo else " Notes:")
    ids = anki_request("findNotes", query=q)
    if not ids:
        note("nothing to translate")
        return
    notes = []
    for i in range(0, len(ids), 500):
        notes += anki_request("notesInfo", notes=ids[i:i + 500])
    note(f"{len(notes)} card(s) need a Russian Notes, groups of {a.group}, {MODEL}/{EFFORT}")

    done = 0
    for start in range(0, len(notes), a.group):
        batch = list(notes[start:start + a.group])
        for attempt in range(ATTEMPTS):
            if not batch:
                break
            body = "\n".join(
                f"{i}: {n['fields']['Word']['value'].strip()} | "
                f"{n['fields']['Sentence']['value'].strip()}"
                for i, n in enumerate(batch, 1))
            reply, usage, cost = ask(body, freqdeck.SYSTEM_RU, MODEL,
                                     what="russian notes", effort=EFFORT)
            got = {}
            for m in freqdeck._RU_LINE.finditer(reply or ""):
                i = int(m.group(1))
                if 1 <= i <= len(batch):
                    got.setdefault(batch[i - 1]["noteId"], m.group(2))
            if got:
                # In one batch: Anki draws a progress panel per write, and there are a hundred.
                items = list(got.items())
                for i in range(0, len(items), 50):
                    anki_request("multi", actions=[
                        {"action": "updateNoteFields",
                         "params": {"note": {"id": nid, "fields": {"Notes": ru}}}}
                        for nid, ru in items[i:i + 50]])
            done += len(got)
            note(f"  {len(got)}/{len(batch)} answered  [{describe(usage)}]")
            batch = [n for n in batch if n["noteId"] not in got]
            if batch:
                note(f"    re-asking {len(batch)}")

    left = len(anki_request("findNotes", query=f'deck:"{d}" -is:suspended Notes:'))
    note(f"\n  translated {done}; still without Notes: {left}")


if __name__ == "__main__":
    main()
