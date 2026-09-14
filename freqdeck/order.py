#!/usr/bin/env python3
"""The order new cards are introduced in — one deck, several source lists, one queue.

Run inside Anki through oneshot_addon (repositioning is a scheduler call, not something
AnkiConnect exposes):

    echo $PWD/freqdeck/order.py > /tmp/gigaku_oneshot_script.txt

Result → /tmp/gigaku_order.json; the positions it replaced → freqdeck/out/order-before.json,
so the whole thing can be put back exactly as it was.

**Why not just append.** A second list would otherwise sit behind the first entirely:
1,000 cards of general spoken German, then 1,000 of YouTube vocabulary, so `teilweise`
(177/M in the user's own subtitles) would wait behind `Truthahn`. The user's ask,
2026-08-27: order the words well, rather than appending every new word at the end.

**The score is expected exposure, and that means a SUM, not a product.** The user watches
both films/series and YouTube, so how often a word reaches them is its rate in one corpus plus
its rate in the other. The geometric mean was tried first and is wrong here: absence in
one corpus is a zero that destroys the product, and absence is common on both sides for
honest reasons — the YouTube corpus is ~6M tokens (a word can miss it by luck: `wachsen`,
second by general frequency, is unattested there and sank to #1546), and the general list
subtracts words Migaku knows and drops what its personal filter cut (`streamen` sank to
#1592). Summed instead, they land at #18 and #95, which is where a reader would put them.

Each corpus is divided by its own median over the deck's words, so neither scale dominates
— `living_score` (film⁰·⁷ × everyday⁰·³, freq/de-living-german.tsv) and per-million rates
in the user's subtitles (freq/de-yt.tsv) are otherwise not comparable numbers.

**One correction, measured:** a word's YouTube rate is trustworthy only as far as it is
spread across channels. `Einatmen` reads 49.7/M off **8** channels and `Ausatmen` 47.1/M
off **4** — yoga videos repeating one instruction, not vocabulary met everywhere. The
YouTube term is therefore damped by `min(1, channels / SPREAD)`, which moves those two to
#826 and #1674 and leaves everything with a real spread untouched (`teilweise` 216
channels, `Staffel` 75 — both unmoved at #2 and #3).
"""
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(
    globals().get("__file__") or os.path.join(os.getcwd(), "freqdeck", "order.py")))
ROOT = os.path.dirname(HERE)
SPREAD = 25          # channels at which a YouTube rate is believed in full
RESULT = "/tmp/gigaku_order.json"
CONFIG_KEY = "gigaku.freqOrder"   # read back by anki/gigaku/features/freq_order.py after recalc
BEFORE = os.path.join(HERE, "out", "order-before.json")


def _table(path, key, value, cast=float):
    """{key column: value column} from a headed tsv, skipping rows that don't parse."""
    out = {}
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        ki, vi = header.index(key), header.index(value)
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) > max(ki, vi) and p[ki]:
                try:
                    out[p[ki]] = cast(p[vi])
                except ValueError:
                    pass
    return out


def scores(words, living, yt_pm, channels, spread=SPREAD):
    """{word: expected-exposure score}. Pure — tests pin it, Anki only consumes it."""
    live = [living[w] for w in words if w in living]
    subs = [yt_pm[w] for w in words if w in yt_pm]
    ml = st.median(live) if live else 1.0
    my = st.median(subs) if subs else 1.0
    out = {}
    for w in words:
        general = living.get(w, 0.0) / ml
        mine = yt_pm.get(w, 0.0) / my * min(1.0, channels.get(w, 0) / spread)
        out[w] = general + mine
    return out


def order_for(words, living, yt_pm, channels):
    """Deck order: best expected exposure first; ties by the word, so it is reproducible."""
    s = scores(words, living, yt_pm, channels)
    return sorted(words, key=lambda w: (-s[w], w)), s


def main():
    from aqt import mw

    out = {"ok": False}
    try:
        living = _table("freq/de-living-german.tsv", "lemma", "living_score")
        yt_pm = _table("freq/de-yt.tsv", "word", "pm")
        channels = _table("freq/de-yt.tsv", "word", "channels", int)

        deck_name = "Frequency 🇩🇪"
        nids = mw.col.find_notes(f'deck:"{deck_name}"')
        by_word = {}
        for nid in nids:
            note = mw.col.get_note(nid)
            word = (note["Word"] or "").strip()
            if word:
                by_word.setdefault(word, []).append(nid)
        ordered, score = order_for(sorted(by_word), living, yt_pm, channels)

        # Only new cards can be repositioned, and only those should be: a card the user
        # has already started is in the scheduler's hands, not this script's.
        before, card_ids = {}, []
        for word in ordered:
            for nid in by_word[word]:
                for card in mw.col.get_note(nid).cards():
                    if card.type == 0:               # CARD_TYPE_NEW
                        card_ids.append(card.id)
                        before[str(card.id)] = card.due
        os.makedirs(os.path.dirname(BEFORE), exist_ok=True)
        with open(BEFORE, "w", encoding="utf-8") as f:
            json.dump(before, f)

        mw.col.sched.reposition_new_cards(
            card_ids=card_ids, starting_from=1, step_size=1,
            randomize=False, shift_existing=False)

        # AnkiMorphs' recalc sets the due of every new card it manages — measured, it
        # overwrote this order the first time (the queue came back `oha, Rubel, Mod`).
        # So the order is also STORED, and the gigaku add-on's `freq_order` feature puts
        # it back after each R. Without that, this holds only until the next recalc.
        # Merge, never clobber: another deck (the sentence-mining skill's) keeps its own
        # order under the same key, and overwriting it would silently cost that deck its
        # replay after every recalc.
        cur = mw.col.get_config(CONFIG_KEY, default=None) or {}
        others = [o for o in (cur.get("orders") or ([cur] if cur.get("cards") else []))
                  if o.get("deck") != deck_name]
        mw.col.set_config(CONFIG_KEY,
                          {"orders": others + [{"deck": deck_name, "cards": card_ids}]})

        out.update(ok=True, notes=len(nids), words=len(by_word), repositioned=len(card_ids),
                   skipped_not_new=sum(len(v) for v in by_word.values()) - len(card_ids),
                   first=[(w, round(score[w], 2)) for w in ordered[:15]],
                   last=[(w, round(score[w], 2)) for w in ordered[-5:]],
                   undo=BEFORE, stored_as=CONFIG_KEY)
    except Exception:
        import traceback
        out["traceback"] = traceback.format_exc()
    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__oneshot__" or __name__ == "__main__":
    main()
