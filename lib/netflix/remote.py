"""The mouse's left and right buttons as a Netflix remote — the daemon's half.

All this does is deliver a keystroke. The behaviour lives in the `chrome/gigaku` extension
(`remote-keys.js`), which already runs on every Netflix page, and the split is not arbitrary:
**fullscreen can only be entered from inside a real user gesture**, so the two halves are "make
a gesture" and "be inside it", and nothing this process can do covers both.

What was measured getting here (live `/watch` tab, 2026-08-04), in the order it was learnt:

* `element.requestFullscreen()` from AppleScript-injected JS is refused — `TypeError:
  Permissions check failed` — on a **visible, focused** tab, with `document.fullscreenEnabled`
  true and `navigator.userActivation.isActive` reading true. A hidden tab was the first
  suspicion and it was wrong; the refusal survives every condition this side can arrange. From
  inside a trusted `keydown` the same call succeeded **8 times out of 8**.
* Netflix's own `f` shortcut cannot be that gesture. All eight presses reached the page
  correctly — `key:"f"`, `code:"KeyF"`, `isTrusted:true`, target BODY — and every one carried
  `defaultPrevented: true` with zero `fullscreenchange` events: something on the watch page
  consumes it. (`ArrowLeft`/`ArrowRight` are consumed the same way; space is not.) That is why
  the right button worked for a few presses and then stopped.
* Before that it never worked at all, for a different reason: a virtual keycode is a *position*
  and the character comes from the active input source, so `kVK_ANSI_F` arrived under the
  Russian layout as `key:"а"`. A function key carries no character and cannot be mistranslated.
* A function key grants user activation like any other (`isActive: true`) and fullscreen from
  its handler succeeded. It is also **inert everywhere else**, which is the property that makes
  posting it blind safe: with Chrome in the background the press does nothing at all, where
  space or `f` would have typed into whatever the user was really working in.
* Which function key, though, is measured rather than assumed — see the keycodes below.

Driving the tab over Apple events was built first and removed. It worked from the terminal and
never from the daemon: macOS refuses Automation *without prompting* to a process whose bundle
carries no `NSAppleEventsUsageDescription`, so every press answered "Not authorized to send
Apple events to Google Chrome". Granting it would have meant editing `GigakuSound.app`'s
Info.plist and re-signing, which changes the ad-hoc cdhash and drops the **Accessibility**
grant the wheel itself depends on. The extension needs no permission at all, so that whole
trade disappeared rather than being paid.
"""

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    kCGHIDEventTap,
)

# kVK_F17 / kVK_F18. Which function keys are usable is **not** a matter of taste here — macOS
# takes several of them above the browser, and a key it takes never reaches the page while still
# firing whatever the system has bound to it. Measured on this Mac with the tab focused and
# visible, one key at a time: F16, F17, F18, F19 and F20 all arrived (`key:"F17"` etc.); **F14
# never arrived** (the system keeps F14/F15 for screen brightness); and **F13 arrived once and
# then stopped arriving**, plain and with Shift alike, which is what was reported as both
# buttons "changing the screen" — a macOS action firing while Chrome saw nothing. An
# intermittent key is disqualified, not debugged. Two plain keys also mean no modifier: the
# Shift that told the buttons apart is gone with the keycode that needed it.
# Must stay in step with `chrome/gigaku/remote-keys.js`, which is the only thing that gives
# these presses meaning.
PLAY_PAUSE_KEYCODE = 64  # kVK_F17
FULLSCREEN_KEYCODE = 79  # kVK_F18


def play_pause() -> str:
    return _tap(PLAY_PAUSE_KEYCODE, "F17")


def fullscreen() -> str:
    return _tap(FULLSCREEN_KEYCODE, "F18")


def _tap(code: int, name: str) -> str:
    """Post one key tap (down+up) at the HID level.

    HID is what makes the event *trusted*, which is the whole point — a synthetic
    `KeyboardEvent` would carry no user activation and the extension's fullscreen call would be
    refused exactly as this module's first version was. No character is stamped on the event
    (`CGEventKeyboardSetUnicodeString` with an empty string): these keys have none, and letting
    the layout supply one is the bug that made the old `f` arrive as "а".

    Whether anything acted on it is deliberately not reported. This process cannot see the page
    — that is the extension's side — and a label claiming success it never checked is worse
    than one that only claims delivery.
    """
    for down in (True, False):
        ev = CGEventCreateKeyboardEvent(None, code, down)
        CGEventKeyboardSetUnicodeString(ev, 0, "")
        # Clear the modifier flags outright. A created keyboard event otherwise picks up
        # whatever the user is physically holding, and a stray Shift or Cmd would turn an inert
        # key into a shortcut in whatever app is in front.
        CGEventSetFlags(ev, 0)
        CGEventPost(kCGHIDEventTap, ev)
    return f"sent {name}"
