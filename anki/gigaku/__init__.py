"""Gigaku — the one Anki add-on: study-morph counter, browse keys, trimmer retunes.

This package replaces the three separate add-ons (study_morph_counter, browser_jk_nav,
cyrillic_shortcuts): one install, one config, one set of helpers instead of three diverging
copies. Features live under `features/`, shared machinery under `core/`, and the **rules
that gigaku's CLI mirrors live in top-level pure modules** — `rules`, `queries`,
`trim_rules` — which import no aqt, so the repo's pytest imports them directly and pins
them against `lib/anki`. Mirror drift between the CLI and the add-on used to be caught by
re-implementing the format inside a test; now it fails the suite structurally.

The aqt guard below is what makes that work: outside Anki (pytest) the package imports
cleanly and simply doesn't wire anything.
"""

try:
    import aqt  # noqa: F401

    _IN_ANKI = True
except ImportError:
    _IN_ANKI = False  # imported by the repo's pytest for rules/queries/trim_rules

if _IN_ANKI:
    from . import entry

    entry.install()
