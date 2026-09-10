"""Keep the Frequency deck's own queue order across an AnkiMorphs recalc.

`freqdeck/order.py` orders that deck by expected exposure — how often a word reaches the
user across the two corpora he actually watches — and repositions its new cards. Measured
2026-08-28: **AnkiMorphs' recalc rewrites the due of every new card it manages**, so that
order lived exactly until the next `R` (the queue came back `oha, Rubel, Mod, …`). Its own
`recalc_move_new_cards_to_the_end` / `recalc_offset_new_cards` switches are off and beside
the point — setting new-card order *is* what recalc does.

So the order is stored in the collection (`gigaku.freqOrder`, written by order.py: the
deck's name and its card ids in order) and put back here after every recalc. Nothing is
recomputed at recalc time — no frequency tables are read, no scoring runs — this only
replays a list, which is why it can safely ride the R key.

The stored list is a claim about cards, and cards change: one may have been studied (it is
no longer new and the scheduler owns it), deleted, or added after the list was written. So
each replay keeps only the ids that are still new (`rules.freq_order_cards`), in their
stored order, and says in the log how many it dropped — a list that has drifted far is the sign to re-run order.py.

Off-switch: `"freq_order": {"enabled": false}`, or clear the stored key. The seam is the
same one `am_recalc` uses (`_update_cards_and_notes`, resolved by name at call time), and
the repositioning is handed to the main thread *after* recalc's own operation finishes —
a write inside another op's transaction is not this feature's business.
"""
from .. import rules
from ..core import addons, conf
from ..core.log import log
from ..core.patching import patch_once

CONFIG_KEY = "gigaku.freqOrder"
_DELAY_MS = 1500          # recalc's op has to land before its due values are overwritten


def _apply():
    from aqt import mw

    try:
        config = mw.col.get_config(CONFIG_KEY, default=None)
        if not config:
            return

        def still_new(cid):
            try:
                return mw.col.get_card(cid).type == 0        # CARD_TYPE_NEW
            except Exception:  # noqa: BLE001 — a deleted card is simply not repositioned
                return False

        for order in rules.freq_orders(config):
            cards = rules.freq_order_cards(order, still_new)
            if not cards:
                continue
            mw.col.sched.reposition_new_cards(
                card_ids=cards, starting_from=1, step_size=1,
                randomize=False, shift_existing=False)
            dropped = len(order.get("cards") or []) - len(cards)
            log(f"freq_order: {len(cards)} cards repositioned in {order.get('deck')!r}"
                + (f", {dropped} no longer new" if dropped else ""))
    except Exception as exc:  # noqa: BLE001 — an order that fails must not cost the recalc
        log(f"freq_order: skipped ({exc!r})")


def _wrap(original):
    def patched(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            from aqt import mw
            from aqt.qt import QTimer

            mw.taskman.run_on_main(lambda: QTimer.singleShot(_DELAY_MS, _apply))
        except Exception as exc:  # noqa: BLE001
            log(f"freq_order: could not schedule ({exc!r})")
        return result

    return patched


def _patch(module):
    patch_once(module, "_update_cards_and_notes", _wrap, "_gigaku_freq_order")


def install():
    if not conf.section("freq_order").get("enabled", True):
        return
    addons.when_recalc_ready(_patch)
