"""Locating the third-party add-ons this one retunes — one strategy each, used everywhere.

MvJ's package is found in sys.modules rather than by importing its folder (the name has a
space in it), and submodules are then imported rather than read off the package, because
MvJ imports several of them only inside functions, so they are often not loaded yet. The
old add-ons kept three copies of this scan and a second, folder-name strategy besides.
"""
import sys
from importlib import import_module

ANKIMORPHS_ADDON_ID = "472573498"
TRIMMER_ADDON_DIR = "Audio Trimmer"


def mvj(path):
    """An MvJ submodule (``actions``, ``services.media``, ``services.merge``,
    ``utils.morphs``)."""
    for name, module in list(sys.modules.items()):
        if name.endswith(".mvj") and getattr(module, "ADDON_NAME", None):
            return import_module(f"{name}.{path}")
    raise LookupError("MvJ add-on not loaded")


def mvj_media_service():
    for name, module in list(sys.modules.items()):
        if name.endswith(".mvj") and getattr(module, "ADDON_NAME", None):
            return import_module(f"{name}.services").media_service
    raise LookupError("MvJ add-on not loaded")


def trimmer():
    """The Audio Trimmer add-on's top-level module.

    Keyed by its folder name, which is also its module name (Anki imports an add-on by
    directory, space and all); the scan is the fallback for a rename, and finds it by the
    function this package actually calls rather than by guessing another name."""
    module = sys.modules.get(TRIMMER_ADDON_DIR)
    if module is None:
        for name, candidate in list(sys.modules.items()):
            if "." not in name and hasattr(candidate, "_trim_audio_for_field"):
                module = candidate
                break
    if module is None:
        raise LookupError("Audio Trimmer add-on not loaded")
    return module


def ankimorphs_recalc():
    """AnkiMorphs' recalc_main module, or None while it is still importing."""
    ankimorphs = sys.modules.get(ANKIMORPHS_ADDON_ID)
    if ankimorphs is None:
        return None
    try:
        return ankimorphs.recalc.recalc_main
    except AttributeError:
        return None


# ── waiting for AnkiMorphs' recalc module (W2, 2026-08-08) ───────────────────
# Two features patch recalc — the counter's post-recalc refill and the language
# scope — and each used to carry its own 20×500ms retry ladder with its own guard
# flag. One ladder now: callbacks queue here, the first profile open resolves the
# module once, and patch_once's sentinels keep any re-registration harmless.

_recalc_callbacks = []
_recalc_module = None
_ladder_hooked = False


def when_recalc_ready(callback):
    """Run ``callback(recalc_main_module)`` once AnkiMorphs has finished importing.

    Callable at install time (before any profile is open) or later; a callback
    registered after resolution fires immediately. A callback that raises loses only
    itself — the other patches still land."""
    global _ladder_hooked
    if _recalc_module is not None:
        _run_recalc_callback(callback, _recalc_module)
        return
    _recalc_callbacks.append(callback)
    if not _ladder_hooked:
        from aqt import gui_hooks
        from aqt.qt import QTimer

        gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(1000, _resolve_recalc))
        _ladder_hooked = True


def _run_recalc_callback(callback, module):
    from .log import log

    try:
        callback(module)
    except Exception as exc:  # noqa: BLE001 — one broken patch must not cost the rest
        log(f"recalc patch failed: {exc!r}")


def _resolve_recalc(attempt=0):
    global _recalc_module
    module = ankimorphs_recalc()
    # `caching` is resolved lazily by AnkiMorphs itself; requiring it means "fully
    # imported", which both patch targets need.
    if module is None or getattr(module, "caching", None) is None:
        if attempt < 20:  # AnkiMorphs may still be importing — retry briefly
            from aqt.qt import QTimer

            QTimer.singleShot(500, lambda: _resolve_recalc(attempt + 1))
        return
    _recalc_module = module
    for callback in _recalc_callbacks:
        _run_recalc_callback(callback, module)
    _recalc_callbacks.clear()


def ankimorphs_toolbar_stats():
    """AnkiMorphs' toolbar_stats module, or None while it is still importing."""
    ankimorphs = sys.modules.get(ANKIMORPHS_ADDON_ID)
    if ankimorphs is None:
        return None
    try:
        return ankimorphs.toolbar_stats
    except AttributeError:
        return None
