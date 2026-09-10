"""The toolbar's known-lemmas counter, split by language: 🇩🇪: n 🇯🇵: m.

AnkiMorphs' `L:` is one number over a table with no language column, and since German
joined the collection (2026-08-06) a single total says nothing — 🇩🇪 7k and 🇯🇵 3.8k read
as an 11k blob. The user asked for the split by flag (his exact shape: 🇩🇪 first).

A runtime patch, NOT an edit to AnkiMorphs' files: that add-on updates from AnkiWeb (one
click on Anki's prompt replaces the folder wholesale), so an edit would quietly vanish —
the same reason the counter hooks recalc through `patch_once` instead of patching the
file. `MorphToolbarStats.update_stats` is wrapped: the original runs first (its early
returns — profile not open, schema changed — leave its own label standing), then the same
lemmas are re-read with AnkiMorphs' own query and threshold (mirrored from
toolbar_stats.py — including its choice of the *inflection* interval for the lemma count,
so our total always equals the L: it replaces) and classified by script via
`rules.lang_of`, the mirror of lib/vocab/ankimorphs.py's `_lang_of`. Any failure keeps
AnkiMorphs' own label — this feature must never cost the toolbar its number.

Every toolbar redraw builds a fresh MorphToolbarStats, so patching the class covers the
profile-open draw, the post-recalc draw and the known-morphs-exporter draw alike.
"""
from aqt import gui_hooks, mw
from aqt.qt import QTimer

from .. import rules
from ..core import addons
from ..core.log import log
from ..core.patching import patch_once


def _wrap(original):
    def patched(self):
        original(self)
        module = addons.ankimorphs_toolbar_stats()
        if module is None:
            return
        try:
            am_db = module.AnkiMorphsDB()
            am_config = module.AnkiMorphsConfig()
            interval = (am_config.interval_for_known_morphs
                        if am_config.toolbar_stats_use_known else 1)
            rows = am_db.con.execute(
                "SELECT DISTINCT lemma FROM Morphs "
                "WHERE highest_inflection_learning_interval >= ?",
                (interval,)).fetchall()
            am_db.con.close()
        except Exception:  # noqa: BLE001 — keep AnkiMorphs' own label, whatever happened
            return
        counts = {}
        for (lemma,) in rows:
            lang = rules.lang_of(lemma)
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        # Seven non-breaking spaces, not ASCII and not an em-space: the label is HTML,
        # which collapses ordinary spaces, and the webview font drew U+2003 narrow —
        # both measured on the live toolbar, both read as one cramped blob.
        self.lemmas = f"🇩🇪: {counts.get('de', 0)}{'\u00a0' * 7}🇯🇵: {counts.get('ja', 0)}"

    return patched


def _patch(attempt=0):
    module = addons.ankimorphs_toolbar_stats()
    if module is None:
        if attempt < 20:  # AnkiMorphs may still be importing — retry briefly
            QTimer.singleShot(500, lambda: _patch(attempt + 1))
        return
    if patch_once(module.MorphToolbarStats, "update_stats", _wrap, "_gigaku_am_toolbar"):
        log("am_toolbar: patched MorphToolbarStats.update_stats")
        if mw is not None:
            mw.toolbar.draw()  # show the split now, not at the next recalc


def install():
    gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(1200, _patch))
