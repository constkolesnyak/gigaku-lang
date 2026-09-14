"""The Frequency deck's queue order surviving an AnkiMorphs recalc.

Measured 2026-08-28: recalc rewrites the due of every new card it manages, so the
repositioning `freqdeck/order.py` does lived only until the next R (the queue came back
`oha, Rubel, Mod, …`). The order is therefore stored in the collection and replayed by
`anki/gigaku/features/freq_order.py`; what that replay must get right is pinned here.
"""
from gigaku import rules


def test_only_cards_that_are_still_new_are_repositioned():
    """A card the user has begun belongs to the scheduler, not to this list."""
    config = {"deck": "Frequency 🇩🇪", "cards": [11, 22, 33, 44]}
    assert rules.freq_order_cards(config, lambda cid: cid != 22) == [11, 33, 44]


def test_the_stored_order_is_kept_exactly():
    config = {"deck": "d", "cards": [5, 3, 9, 1]}
    assert rules.freq_order_cards(config, lambda cid: True) == [5, 3, 9, 1]


def test_nothing_stored_means_nothing_done():
    for empty in (None, {}, {"deck": "d"}, {"deck": "d", "cards": []}, "not a dict"):
        assert rules.freq_order_cards(empty, lambda cid: True) == []


def test_every_card_gone_is_not_an_error():
    assert rules.freq_order_cards({"deck": "d", "cards": [1, 2]}, lambda cid: False) == []
