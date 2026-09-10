"""One dialog for every key: gigaku's rows inside MvJ's Settings ▸ Hotkeys tab.

Two settings screens for one keyboard is how an evening got spent on keys that silently
did nothing. The user sets keys in MvJ's Hotkeys tab, so that is where all of them live
now — this add-on's rows are added to it, and the rows MvJ has but never shows are added
too. Nothing about a key is decided in code any more.

Three patches, each guarded, each costing only itself if it fails:

* **`MvjConfigManager._dict_to_config`** — MvJ filters its saved hotkeys through an
  `allowed_hotkeys` set on load, and four of its own rows are not in it:
  `browser_navigate_up`/`_down`, `browser_focus_sentence` and
  `play_locked_definition_audio`. Whatever the config file says, those four are dropped
  and the dataclass defaults win: `i`, `e`, `n`. That is why `i` could not be freed for
  View Context by editing anything — and why it looked like this add-on had hardcoded
  them. Every field of `HotkeyConfig` present in the saved config is put back after the
  original has run, so a saved value means what it says.
* **`HotkeysTab.__init__`** — a third column: MvJ's four unshown rows, then this add-on's
  own, under a heading that says whose they are.
* **`HotkeysTab.load` / `SettingsDialog._on_accept`** — this add-on's rows read and write
  *its* config, never MvJ's. MvJ's `load` does `getattr(config, key)` and its `gather`
  does `HotkeyConfig(**values)`, so a foreign key in `_edits` would raise in both; ours
  are kept in a dict of their own. MvJ's four go in `_edits`, where they belong — they are
  its fields, and its own machinery saves them once the loader stops dropping them.
"""
from aqt import mw
from aqt.qt import (QGridLayout, QHBoxLayout, QKeySequence, QKeySequenceEdit, QLabel,
                    QPushButton, Qt, QWidget)

from ..core import addons, conf
from ..core.log import log
from ..core.patching import patch_once

# MvJ's own rows that its dialog does not show and its loader drops.
_MVJ_UNSHOWN = (
    ("browser_navigate_down", "Row Down (MvJ) ⬇️"),
    ("browser_navigate_up", "Row Up (MvJ) ⬆️"),
    ("browser_focus_sentence", "Focus Sentence 🎯"),
    ("play_locked_definition_audio", "Play Locked Def 🔒"),
)

# This add-on's keys: (config section, key in it, label). The same values the `keys` and
# `nav` sections hold — this is a second way in, not a second source.
_GIGAKU = (
    ("nav", "goto_hotkey", "Open Queue 🇯🇵"),
    ("nav", "goto_de_hotkey", "Open Queue 🇩🇪"),
    ("nav", "alternates_hotkey", "Alternates Toggle 🔎"),
    ("keys", "row_down_hotkey", "Row Down ⬇️"),
    ("keys", "row_up_hotkey", "Row Up ⬆️"),
    ("keys", "known_toggle_hotkey", "Known / Undo ✅"),
    ("keys", "flag_translate_hotkey", "Flag + Translate 🚩"),
    ("keys", "trim_audio_hotkey", "Trim Audio ✂️"),
    ("keys", "ask_question_hotkey", "Ask Question 🗣️"),
)


def _unfilter_hotkeys():
    """Stop MvJ dropping four of its own hotkey rows on load.

    It builds `HotkeyConfig(**{k: v for k, v in saved.items() if k in allowed_hotkeys})`,
    and that set is missing four fields the dataclass has. The saved value for those is
    discarded every load, so the defaults `i`, `e`, `n` are unreachable from any file or
    dialog. Read back off the live browser after `i` refused to work: two claims on it,
    both MvJ's — the View Context the user had just set, and this.

    Every `HotkeyConfig` field present in the saved config is put back, rather than the
    four by name: a row MvJ adds later then works the day it appears."""
    try:
        import dataclasses

        cls = addons.mvj("config").MvjConfigManager

        def wrap(original):
            def _dict_to_config(self, data):
                config = original(self, data)
                try:
                    saved = (data or {}).get("hotkeys") or {}
                    hotkeys = config.hotkeys
                    known = {f.name for f in dataclasses.fields(type(hotkeys))}
                    for name, value in saved.items():
                        if name in known and getattr(hotkeys, name, None) != value:
                            setattr(hotkeys, name, value)
                except Exception as exc:  # noqa: BLE001 — never break MvJ's config load
                    log(f"hotkey ui: restoring dropped rows FAILED {exc!r}")
                return config

            return _dict_to_config

        if patch_once(cls, "_dict_to_config", wrap, "_gigaku_hotkeys_unfiltered"):
            log("hotkey ui: MvJ's dropped hotkey rows are settable again")
    except Exception as exc:  # noqa: BLE001
        log(f"hotkey ui: unfilter FAILED {exc!r}")


def _gigaku_value(section, key):
    return str(conf.section(section).get(key) or "")


def _save_gigaku(values):
    """Write this add-on's rows back into its own config — one write, on OK."""
    try:
        current = mw.addonManager.getConfig("gigaku") or {}
        changed = []
        for (section, key), value in values.items():
            block = dict(current.get(section) or {})
            if str(block.get(key) or "") != value:
                block[key] = value
                current[section] = block
                changed.append(f"{section}.{key}={value or '(none)'}")
        if changed:
            mw.addonManager.writeConfig("gigaku", current)
            log(f"hotkey ui: saved {', '.join(changed)}")
    except Exception as exc:  # noqa: BLE001
        log(f"hotkey ui: save FAILED {exc!r}")


def _row(parent, grid, index, label_text):
    """One label + key editor + clear button, laid out like MvJ's own rows."""
    container = QWidget(parent)
    row_layout = QHBoxLayout(container)
    row_layout.setContentsMargins(0, 0, 0, 0)
    row_layout.setSpacing(2)
    edit = QKeySequenceEdit(container)
    edit.setMinimumWidth(100)
    edit.setMaximumWidth(300)
    clear = QPushButton("✕", container)
    clear.setFixedWidth(22)
    clear.setToolTip("Clear hotkey")
    clear.clicked.connect(edit.clear)
    row_layout.addWidget(edit)
    row_layout.addWidget(clear)
    row_layout.addStretch()
    label = QLabel(label_text + ":", parent)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    grid.addWidget(label, index, 0)
    grid.addWidget(container, index, 1)
    return edit


def _heading(parent, grid, index, text):
    label = QLabel(text, parent)
    label.setStyleSheet("font-weight: bold;")
    grid.addWidget(label, index, 0, 1, 2)


def _add_column(tab):
    """A third column: MvJ's unshown rows, then this add-on's."""
    column = QWidget(tab)
    grid = QGridLayout(column)
    grid.setVerticalSpacing(5)
    grid.setHorizontalSpacing(4)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)

    index = 0
    _heading(tab, grid, index, "MvJ (browser)")
    index += 1
    for key, label in _MVJ_UNSHOWN:
        # Into MvJ's own dict: these are its fields, so its load/gather handle them.
        tab._edits[key] = _row(column, grid, index, label)
        index += 1

    _heading(tab, grid, index, "gigaku")
    index += 1
    tab._gigaku_edits = {}
    for section, key, label in _GIGAKU:
        tab._gigaku_edits[(section, key)] = _row(column, grid, index, label)
        index += 1

    # `main_layout` is a QVBoxLayout whose first item is the row of columns; put ours at
    # its end, before the stretch that absorbs the spare width.
    layout = tab.layout()
    columns = layout.itemAt(0).layout() if layout and layout.count() else None
    if columns is not None:
        columns.insertWidget(max(0, columns.count() - 1), column)
    elif layout is not None:
        layout.addWidget(column)


def _extend_dialog():
    try:
        settings = addons.mvj("settings")

        def wrap_init(original):
            # *args/**kwargs, never a guessed signature — a wrapper declaring
            # (self, parent) on SettingsDialog crashed the whole dialog when MvJ's
            # constructor grew a keyword (settings_ui, 2026-08-06). Same insurance here.
            def __init__(self, *args, **kwargs):
                original(self, *args, **kwargs)
                try:
                    _add_column(self)
                except Exception as exc:  # noqa: BLE001 — the dialog must still open
                    log(f"hotkey ui: adding rows FAILED {exc!r}")

            return __init__

        def wrap_load(original):
            def load(self, config):
                original(self, config)
                try:
                    for (section, key), edit in getattr(self, "_gigaku_edits", {}).items():
                        value = _gigaku_value(section, key)
                        edit.setKeySequence(QKeySequence(value)) if value else edit.clear()
                except Exception as exc:  # noqa: BLE001
                    log(f"hotkey ui: loading our rows FAILED {exc!r}")

            return load

        def wrap_accept(original):
            def _on_accept(self):
                tab = getattr(self, "_hotkeys_tab", None)
                edits = getattr(tab, "_gigaku_edits", {}) if tab is not None else {}
                # Gathered before the original runs: it closes the dialog, and a closed
                # dialog's widgets are not something to be reading keys out of.
                values = {ref: edit.keySequence().toString() for ref, edit in edits.items()}
                original(self)
                _save_gigaku(values)

            return _on_accept

        if patch_once(settings.HotkeysTab, "__init__", wrap_init, "_gigaku_rows"):
            log("hotkey ui: added our rows to MvJ's Hotkeys tab")
        patch_once(settings.HotkeysTab, "load", wrap_load, "_gigaku_rows_load")
        patch_once(settings.SettingsDialog, "_on_accept", wrap_accept, "_gigaku_rows_save")
    except Exception as exc:  # noqa: BLE001
        log(f"hotkey ui: extending the dialog FAILED {exc!r}")


def install():
    _unfilter_hotkeys()
    _extend_dialog()
