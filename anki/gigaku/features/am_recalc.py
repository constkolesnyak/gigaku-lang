"""Which languages a recalc touches — the pause switch AnkiMorphs doesn't have.

The user pauses a language (2026-08-07: Japanese) and R should stop grinding and
rewriting its 460k cards while German stays live. AnkiMorphs' own per-row read/modify
ticks live in *its* settings, which this project reads and never writes — so the switch
is gigaku's (`recalc · ja` / `recalc · de` in the generated Gigaku section) and the
interception is a runtime patch, like the toolbar split.

The seam is NOT `recalc` itself: the R key and the toolbar link hold a captured
reference (`triggered.connect(recalc_main.recalc)`), so patching the module attribute
would drive the menu entry and miss the key. What `recalc()` resolves by name at call
time are `caching.cache_anki_data` (given the read rows) and `_update_cards_and_notes`
(the modify rows) — both with no other caller, so the scope is recalc's by construction
and the reviewer-side matchers (`get_matching_*_filter`) are untouched.

What a paused language keeps, read off AnkiMorphs 6.2.0's code: `cache_anki_data`
opens with `drop_all_tables`, so its per-card rows do leave ankimorphs.db — but its
KNOWN morphs return on the same pass, because the known-morphs folder is read globally
(`_get_known_morphs_files` rglobs the whole dir) and the daily backup keeps those CSVs
converged with the full known set. The toolbar count and `gigaku import`'s keep-shield
therefore survive. In the collection nothing is written at all: fields and _card-status
tags freeze exactly as the last full recalc left them. Un-pause = tick the box, press R.

Every scoped recalc says so in a tooltip — a pause that shows itself on every R can't
be forgotten, and the tooltip going missing after an AnkiMorphs update is the visible
sign the seam moved (an unrecognized signature passes through untouched: a broken pause
must cost the pause, never the recalc).
"""
from .. import rules
from ..core import addons, conf
from ..core.log import log
from ..core.patching import patch_once

_FLAGS = {"ja": "🇯🇵", "de": "🇩🇪"}


def _scope():
    """(selection, ja_notetype, de_notetype); a None selection intercepts nothing."""
    cfg = conf.section("recalc")
    selected = rules.recalc_selection(bool(cfg.get("ja", True)), bool(cfg.get("de", True)))
    ja_nt = (conf.section("counter").get("notetype") or "🇯🇵 MvJ").strip()
    media = conf.mvj_config().get("media") or {}
    de_nt = (media.get("german_notetype") or "🇩🇪 German").strip()
    return selected, ja_nt, de_nt


def _filtered(filters, announce):
    selected, ja_nt, de_nt = _scope()
    if selected is None:
        return filters
    kept = [f for f in filters
            if rules.recalc_keeps(getattr(f, "note_type", None), selected, ja_nt, de_nt)]
    if announce and len(kept) != len(filters):
        _say(selected)
    return kept


def _say(selected):
    """Named on every scoped R. Runs on recalc's background thread — tooltip via main."""
    paused = "".join(_FLAGS[lang] for lang in ("ja", "de") if lang not in selected)
    log(f"recalc scoped to {sorted(selected)}")
    try:
        from aqt import mw
        from aqt.utils import tooltip

        mw.taskman.run_on_main(
            lambda: tooltip(f"Recalc: {paused} paused (gigaku recalc languages)", period=5000))
    except Exception:  # noqa: BLE001 — the announcement must never cost the recalc
        pass


def _rescope(args, kwargs, index, key, announce):
    """Filter the note-filter list wherever the call put it; unknown shapes pass through."""
    if key in kwargs:
        kwargs = dict(kwargs)
        kwargs[key] = _filtered(kwargs[key], announce)
    elif len(args) > index and isinstance(args[index], list):
        args = list(args)
        args[index] = _filtered(args[index], announce)
        args = tuple(args)
    return args, kwargs


def _wrap_cache(original):
    def patched(*args, **kwargs):  # signature never assumed — the SettingsDialog lesson
        args, kwargs = _rescope(args, kwargs, 1, "read_enabled_config_filters", announce=True)
        return original(*args, **kwargs)

    return patched


def _wrap_update(original):
    def patched(*args, **kwargs):
        args, kwargs = _rescope(args, kwargs, 1, "modify_enabled_config_filters", announce=False)
        return original(*args, **kwargs)

    return patched


def _patch_scope(module):
    """The two scoping patches — via the shared ladder (`addons.when_recalc_ready`),
    which used to exist twice (W2, 2026-08-08); the ladder guarantees `caching`."""
    patch_once(module.caching, "cache_anki_data", _wrap_cache, "_gigaku_recalc_scope")
    patch_once(module, "_update_cards_and_notes", _wrap_update, "_gigaku_recalc_scope")


def install():
    addons.when_recalc_ready(_patch_scope)
