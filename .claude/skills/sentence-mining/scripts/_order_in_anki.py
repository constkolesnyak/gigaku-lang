"""Runs INSIDE Anki (via oneshot_addon): reposition one deck's new cards and store the order.

Reads /tmp/gigaku_order_payload.json — {"deck": name, "cards": [id, ...]} — repositions those
cards in that order, then MERGES the order into `gigaku.freqOrder` so the gigaku add-on's
freq_order feature replays it after every AnkiMorphs recalc. Merging matters: the key also
holds the Frequency deck's order, and clobbering it would silently cost that deck its replay.
"""
import json

out = {}
try:
    from aqt import mw
    # Never leave Anki in the reviewer: a rebuild can delete the card under it,
    # and then Anki crashes with NotFoundError "No such card".
    try:
        mw.moveToState("deckBrowser")
    except Exception:
        pass
    payload = json.load(open("/tmp/gigaku_order_payload.json", encoding="utf-8"))
    deck, cards = payload["deck"], payload["cards"]
    still_new = [c for c in cards if mw.col.get_card(c).type == 0]
    mw.col.sched.reposition_new_cards(card_ids=still_new, starting_from=1, step_size=1,
                                      randomize=False, shift_existing=False)
    KEY = "gigaku.freqOrder"
    cur = mw.col.get_config(KEY, default=None) or {}
    # Keep an entry only while its cards are still where it says they are. "Does the deck
    # exist" is the wrong test: turning a flat deck into a parent leaves the name behind, and
    # its stale order goes on replaying old positions over the very cards that moved into the
    # subdeck.
    def still_owns(o):
        want = set(o.get("cards") or [])
        if not want:
            return False
        # `deck:"X"` matches X's SUBDECKS too, so a parent still "owns" the cards that moved
        # down into one — which is exactly the case this is meant to detect. Exclude them.
        name = o.get("deck")
        q = f'deck:"{name}" -deck:"{name}::*"'
        return any(cid in want for cid in mw.col.find_cards(q))

    others = [o for o in (cur.get("orders") or ([cur] if cur.get("cards") else []))
              if o.get("deck") != deck and still_owns(o)]
    mw.col.set_config(KEY, {"orders": others + [{"deck": deck, "cards": still_new}]})
    out.update(ok=True, repositioned=len(still_new), skipped=len(cards) - len(still_new),
               decks_stored=[o.get("deck") for o in others] + [deck])
except Exception:
    import traceback
    out["traceback"] = traceback.format_exc()
json.dump(out, open("/tmp/gigaku_order.json", "w"))
