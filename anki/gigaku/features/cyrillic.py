"""Make Anki's shortcuts work on a Cyrillic keyboard layout (was cyrillic_shortcuts).

Qt matches shortcuts on QKeyEvent.key(), which is the *character the layout produced*: on
the Russian layout the physical J key reports Cyrillic О, so every Latin-letter shortcut
goes dead at once — Anki's own, every add-on's, and ours. Qt offers no per-layout fallback
on macOS, and nativeScanCode() is unavailable there, but nativeVirtualKey() returns the
Carbon key code of the *physical* key, which is layout-independent and enough to recover
the Latin key.

An application-wide event filter rewrites those events. It acts while the event is on its
way to the QWindow, which is ahead of Qt's shortcut machinery, so the substituted event
then goes through ShortcutOverride and QShortcutMap exactly as a real Latin keypress
would. Nothing has to be registered twice, and shortcuts added later by any add-on are
covered for free.

Typing Russian is left alone. A bare key is only rewritten when the focused object does
not accept text input — asked via Qt's own ImEnabled query, so a focused note field counts
as text and the reviewer's webview does not. Shift is not treated as a command modifier,
so capitals still type. With Cmd or Ctrl held there is nothing to type, so those are
always rewritten.
"""
from aqt import gui_hooks
from aqt.qt import QApplication, QCoreApplication, QEvent, QKeyEvent, QObject, Qt

from ..core.log import log

# Qt reports non-Latin keys as the Unicode codepoint of the uppercase character.
_CYRILLIC = range(0x0400, 0x0500)

_COMMAND_MODIFIERS = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier

# macOS Carbon virtual key codes (kVK_ANSI_*) — physical position, independent of the
# active layout. Digits are omitted: they are the same on both layouts, so Qt already
# reports them as Latin and they never reach the rewrite.
_PHYSICAL_KEYS = {
    0x00: "a", 0x01: "s", 0x02: "d", 0x03: "f", 0x04: "h", 0x05: "g",
    0x06: "z", 0x07: "x", 0x08: "c", 0x09: "v", 0x0B: "b", 0x0C: "q",
    0x0D: "w", 0x0E: "e", 0x0F: "r", 0x10: "y", 0x11: "t", 0x1F: "o",
    0x20: "u", 0x22: "i", 0x23: "p", 0x25: "l", 0x26: "j", 0x28: "k",
    0x2D: "n", 0x2E: "m",
    0x1E: "]", 0x21: "[", 0x27: "'", 0x29: ";", 0x2A: "\\",
    0x2B: ",", 0x2C: "/", 0x2F: ".", 0x32: "`",
}


def _focus_accepts_text():
    """Whether the focused object is somewhere the user is typing.

    Errs towards True: losing a shortcut is a nuisance, but swallowing a letter mid-word
    in a note field is data loss."""
    focus = QApplication.focusObject()
    if focus is None:
        return False
    try:
        return bool(focus.inputMethodQuery(Qt.InputMethodQuery.ImEnabled))
    except Exception:  # noqa: BLE001
        return True


class _CyrillicShortcuts(QObject):
    def eventFilter(self, obj, event):
        # Only on the way to the window: that is the point before Qt looks for a matching
        # shortcut. Filtering the later delivery to the widget too would rewrite the same
        # press twice.
        if not obj.isWindowType():
            return super().eventFilter(obj, event)
        if event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return super().eventFilter(obj, event)
        if event.key() not in _CYRILLIC:
            return super().eventFilter(obj, event)

        latin = _PHYSICAL_KEYS.get(event.nativeVirtualKey())
        if latin is None:
            return super().eventFilter(obj, event)

        modifiers = event.modifiers()
        if not (modifiers & _COMMAND_MODIFIERS) and _focus_accepts_text():
            return super().eventFilter(obj, event)

        text = latin.upper() if modifiers & Qt.KeyboardModifier.ShiftModifier else latin
        replacement = QKeyEvent(
            event.type(),
            ord(latin.upper()),  # Qt key codes for ASCII are the uppercase codepoint
            modifiers,
            text,
            event.isAutoRepeat(),
            1,
        )
        log(f"cyrillic: {event.text()!r} (vk={event.nativeVirtualKey():#04x}) -> {latin!r}")
        # The replacement is Latin, so it passes straight back through this filter.
        QCoreApplication.sendEvent(obj, replacement)
        return True


_filter = _CyrillicShortcuts()


def _install_filter():
    app = QApplication.instance()
    if app is not None:
        app.installEventFilter(_filter)


def install():
    _install_filter()
    # Add-ons load before the app is guaranteed to exist; retry once it certainly does.
    gui_hooks.main_window_did_init.append(_install_filter)
