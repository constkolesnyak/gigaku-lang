#!/usr/bin/env python3
"""Insert the finished draft into Anki as 🇩🇪 German notes.

    python3 scripts/push.py --dry      # print exactly what would be sent
    python3 scripts/push.py

Reports per-card success but never aborts the batch: a late-detected duplicate must not stop
the other cards from landing.

Two things here are load-bearing and easy to get wrong:

**`[audio:…]`, never `[sound:…]`.** Since 2026-08-13 the 🇩🇪 note type renders MvJ's own
template, whose front-side JS scans the Sentence Audio div for `/\\[audio:([^\\]]+)\\]/g` and
builds the player from what it finds. `[sound:]` survives only inside an `.apkg`, where MvJ's
importer rewrites it on the way in. A note written with `[sound:]` here is silent.

**`Image` is never written.** It is not in `field_map`, so it is never sent — the 🇩🇪 front and
back both wrap it in `{{#Image}}`, so an empty Image simply renders nothing. (The Japanese
skill wrote `。` into an empty picture field to stop its own note type replaying the sentence
audio on the back through a `{{^picture}}` branch. This note type has no such branch; copying
that filler here would put a stray `。` on every German card.)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import load_config, work_dir  # noqa: E402
from _anki import anki_request             # noqa: E402


def note(msg):
    print(msg, file=sys.stderr, flush=True)


def source_link(candidate):
    """The episode URL seeked to the sentence — `Context` is rendered by no template, so it
    is the one field where provenance can live without changing what the card looks like."""
    return f"{candidate['url']}&t={candidate['sentence_start_ms'] // 1000}s"


def load_reuse(wd):
    """{word: "[audio:…]"} kept from a previous build of this deck.

    A word's recording does not depend on which sentence was chosen for it, and Commons
    limits by count (~60 files/hour), so a rebuild that re-downloaded them would cost hours
    for nothing. Written by the rebuild before the old deck is deleted.
    """
    p = wd / "wordaudio-reuse.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def build_note(cfg, candidate, reuse=None):
    fm = cfg["field_map"]
    values = {
        "word": candidate["lemma"],
        "sentence": candidate["sentence"],
        "sentence_audio": f"[audio:{candidate['sentence_audio_file']}]",
        "source": source_link(candidate),
    }
    fields = {fm[role]: v for role, v in values.items() if fm.get(role)}
    kept = (reuse or {}).get(candidate["lemma"])
    if kept:
        fields["Word Audio"] = kept
    # NO i-level tag. It used to write i1/i2/i3 from the Migaku diff, and that tag lied:
    # measured 2026-09-04 against a recalc, only 72 of 159 cards tagged `i1` were really
    # i+1. It counts only content words and treats grade-gate words as known, while
    # AnkiMorphs counts every morph — so the two never agreed and the wrong one was the one
    # on the card. AnkiMorphs writes the real verdict itself (`_card-status::i+1`) on the
    # next recalc, which `recalc.py` runs at the end of a build. The i-level stays in
    # draft.json for debugging, where it can't be mistaken for the truth.
    # ONE tag, and it is the provenance handle. The episode (`p03`) and the channel
    # (its name, lowercased) used to ride along too and were pure tag-tree clutter: the episode URL,
    # seeked to the sentence, is already in `Context`. The i-level is AnkiMorphs' to write
    # (`_card-status::*`) — see below.
    tags = list(cfg["tags"])
    return {"deckName": candidate["deck"], "modelName": cfg["note_type"],
            "fields": fields, "tags": tags, "options": {"allowDuplicate": False}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    cfg = load_config()
    wd = work_dir(cfg)
    data = json.loads((wd / "draft.json").read_text(encoding="utf-8"))
    ready = [c for c in data["candidates"] if c.get("sentence_audio_file")]
    if a.limit:
        ready = ready[:a.limit]
    if not ready:
        sys.exit("no candidates have audio — run generate_media.py first")

    reuse = load_reuse(wd)
    if reuse:
        note(f"  reusing {sum(1 for c in ready if c['lemma'] in reuse)} word recording(s) "
             f"from a previous build")
    notes = [build_note(cfg, c, reuse) for c in ready]
    if a.dry:
        print(json.dumps(notes[:5], ensure_ascii=False, indent=2))
        note(f"\n--dry: {len(notes)} notes would be sent "
             f"(first 5 shown), decks {sorted({n['deckName'] for n in notes})}")
        return

    for d in sorted({n["deckName"] for n in notes}):
        anki_request("createDeck", deck=d)      # idempotent

    # Ask first, then send only what Anki will take. `addNotes` does NOT degrade gracefully:
    # given one unaddable note it fails the WHOLE batch with an error list, so 395 good cards
    # were lost to 34 duplicates. The duplicate check fires on the note type's FIRST field,
    # which for 🇩🇪 German is `Sentence`, not `Word` — analyze.py gives each word a sentence of
    # its own for that reason, and this is the backstop.
    verdicts = anki_request("canAddNotesWithErrorDetail", notes=notes)
    addable = [(c, n) for c, n, v in zip(ready, notes, verdicts) if v.get("canAdd")]
    refused = [(c, v.get("error", "?")) for c, v in zip(ready, verdicts) if not v.get("canAdd")]
    if refused:
        note(f"  {len(refused)} refused by Anki before sending:")
        for c, why in refused[:10]:
            note(f"    {c['lemma']}: {why}")
        if len(refused) > 10:
            note(f"    … and {len(refused) - 10} more")
    if not addable:
        sys.exit("nothing left to add")

    ids = anki_request("addNotes", notes=[n for _, n in addable])
    added, failed = 0, [c["lemma"] for c, _ in refused]
    for (c, _), nid in zip(addable, ids):
        if nid is None:
            failed.append(c["lemma"])
        else:
            c["note_id"] = nid
            added += 1
    (wd / "draft.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    note(f"\nadded {added}/{len(notes)} notes")
    if failed:
        note(f"  not added ({len(failed)}): {failed[:15]}"
             + (" …" if len(failed) > 15 else ""))
    from collections import Counter
    for d, n in Counter(c["deck"] for c in ready if c.get("note_id")).most_common():
        note(f"  → {d}: {n}")


if __name__ == "__main__":
    main()
