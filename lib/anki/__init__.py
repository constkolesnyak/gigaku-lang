"""`gigaku clarity` — how obvious is an i+1 card's unknown word from its sentence.

Deliberately imports nothing, like lib/subs and lib/netflix: the pure modules here
(prompt, select, store) must stay importable — by each other and by the tests — without
shelling out to `claude` or reaching for a running Anki.
"""
