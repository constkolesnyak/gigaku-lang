#!/usr/bin/env python3
"""Order the deck by how often each word occurs IN THE CHANNEL it was mined from.

    python3 scripts/order.py            # rank, reposition, store the order
    python3 scripts/order.py --dry      # just print the ranking

`freq/de-yt.tsv` ranks spoken German across many channels, which is the right list for
deciding whether a word is worth a card at all. It is the wrong list for deciding what to
study first out of THIS deck: these cards come from one person talking about one subject, and
what they say often is what you will meet again on the next episode. `Hauptcharakter` occurs 32
times across this playlist and `Backe` once — no general frequency list will tell you that.

So the count is taken over the deck's own transcripts, lemmatised with the same spaCy model
everything else here uses, and the deck's new cards are repositioned most-frequent-first.

⛔ The order is also STORED. AnkiMorphs' recalc rewrites the due of every new card it manages,
so a one-shot reposition lasts exactly until the next R. `gigaku.freqOrder` is replayed by the
gigaku add-on after each recalc — and this writes into it by MERGING, because the Frequency
deck keeps its own order under the same key.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _german import ensure_spacy                                     # noqa: E402
ensure_spacy()

from _config import load_config, deck_main, work_dir                  # noqa: E402
from _anki import anki_request, quiesce                                        # noqa: E402
from _german import CONTENT_POS, load_nlp                             # noqa: E402
import _oneshot                                                       # noqa: E402

PAYLOAD = "/tmp/gigaku_order_payload.json"
RESULT = "/tmp/gigaku_order.json"


def note(m):
    print(m, file=sys.stderr, flush=True)


def channel_counts(cfg, nlp):
    """{lemma: occurrences} over every sentence of every episode — the channel's own frequency."""
    wd = work_dir(cfg)
    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    texts = []
    for m in manifest:
        texts += [s["text"] for s in
                  json.loads(open(m["transcript"], encoding="utf-8").read())["sentences"]]
    cnt = Counter()
    for doc in nlp.pipe(texts, batch_size=256):
        for t in doc:
            if t.pos_ in CONTENT_POS and t.is_alpha:
                cnt[t.lemma_.lower()] += 1
                cnt[t.text.lower()] += 0          # keep surfaces addressable below
    surf = Counter()
    for doc in nlp.pipe(texts, batch_size=256):
        for t in doc:
            if t.is_alpha:
                surf[t.text.lower()] += 1
    note(f"  {len(texts)} sentences, {len(cnt)} lemmas, {sum(cnt.values())} content tokens")
    return cnt, surf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    cfg = load_config()
    d = deck_main(cfg)

    nlp = load_nlp(cfg)
    cnt, surf = channel_counts(cfg, nlp)

    ids = anki_request("findNotes", query=f'deck:"{d}"')
    notes = []
    for i in range(0, len(ids), 500):
        notes += anki_request("notesInfo", notes=ids[i:i + 500])
    ranked = []
    for n in notes:
        w = n["fields"]["Word"]["value"].strip()
        # A separable verb was merged from its two halves, so the transcript never contains
        # the joined lemma; fall back to the surface count so it does not sort last by accident.
        c = cnt.get(w.lower()) or surf.get(w.lower()) or 0
        ranked.append((c, w, n["cards"]))
    ranked.sort(key=lambda t: (-t[0], t[1].lower()))
    note("  most frequent in this channel: "
         + ", ".join(f"{w}({c})" for c, w, _ in ranked[:12]))

    cards = [cid for _, _, cs in ranked for cid in cs]
    if a.dry:
        note(f"\n  --dry: {len(cards)} cards would be repositioned in {d!r}")
        return

    json.dump({"deck": d, "cards": cards}, open(PAYLOAD, "w"))
    _oneshot.run(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_order_in_anki.py"), RESULT, clean=())
    try:
        deadline = time.time() + a.timeout
        while time.time() < deadline and not os.path.exists(RESULT):
            time.sleep(5)
        if not os.path.exists(RESULT):
            sys.exit("the one-shot runner never ran — see /tmp/gigaku_oneshot.log")
        res = json.load(open(RESULT))
    finally:
        _oneshot.cleanup()
    if not res.get("ok"):
        sys.exit(f"ordering failed inside Anki: {res.get('traceback', res)}")
    note(f"\n  repositioned {res['repositioned']} card(s) in {d!r}"
         + (f", {res['skipped']} no longer new" if res.get("skipped") else ""))
    note(f"  stored orders now cover: {res['decks_stored']}")
    if not a.no_sync:
        # The sync belongs HERE, not in recalc.py. recalc rewrites every new card's due and
        # the add-on replays the stored order ~1.5 s AFTER recalc's op lands, so a sync fired
        # at the end of recalc goes out with AnkiMorphs' order and the phone gets a deck that
        # is not the one on screen — which is exactly what happened: locally `Hauptcharakter`
        # sat at due 1 while the phone opened on a card 71 places down.
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
