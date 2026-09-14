#!/usr/bin/env python3
"""Turn a physical mouse into a media remote: wheel controls the Apple TV's
volume, left button plays/pauses, right button toggles fullscreen.

Scroll up → Apple TV volume up, scroll down → volume down, via pyatv
(lib/appletv/control.py) over a persistent connection held by a background worker thread
that runs its own asyncio loop. The event-tap callback only enqueues a command
and returns immediately, so a slow/unreachable Apple TV never stalls the tap.

When the Apple TV can't be reached at all, the step goes to the TV's own remote
instead of nowhere (lib/volume.py owns that fallback and the way back). It is the
same speaker either way — the Apple TV only relays volume to the TV over HDMI-CEC.

The tap is active (not listen-only) so it can consume events:
- Mouse-wheel scrolls drive volume and are swallowed so the Mac doesn't also
  scroll the focused window.
- Physical-mouse left clicks play/pause the Netflix tab and right clicks toggle
  its fullscreen (lib/netflix/remote.py); both are swallowed so the mouse never
  clicks the UI or pops a context menu.
Trackpad input is left completely alone: trackpad scrolls (which carry a
scroll/momentum phase) and trackpad clicks (kCGMouseEventSubtype == 3) pass
through untouched, distinguishing it from the mouse.

Volume targets the Apple TV (pyatv Companion) — pair it once with
scripts/pair_appletv.py; it's then found automatically by its stable identifier (no
hardcoded IP). The fallback TV is paired once too (`uv run python -m
lib.samsung.control`) and found by its hardware MAC. The buttons address the Netflix
**tab**, found by URL: they used
to synthesize a spacebar and an `f` at the HID level and hope the right window
had focus, which is precisely what this daemon prevents — it swallows the click,
so the mouse can never focus the Netflix window, and the keys landed in whatever
the user was really working in. See lib/netflix/remote.py for what each button
now does and what it costs in focus.

Set GIGAKU_SOUND_DEBUG=1 to log every raw scroll/click event before filtering.
Runs until Ctrl+C.
"""

import asyncio
import faulthandler
import os
import queue
import signal
import sys
import threading
import time

faulthandler.enable()

_DEBUG = os.environ.get("GIGAKU_SOUND_DEBUG") == "1"

from Quartz import (
    CGEventGetIntegerValueField,
    CGEventMaskBit,
    CGEventTapCreate,
    CGEventTapEnable,
    CFMachPortCreateRunLoopSource,
    CFMachPortIsValid,
    CFRunLoopAddSource,
    CFRunLoopAddTimer,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    CFRunLoopTimerCreate,
    kCFAllocatorDefault,
    kCFRunLoopCommonModes,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventRightMouseDown,
    kCGEventRightMouseDragged,
    kCGEventRightMouseUp,
    kCGEventScrollWheel,
    kCGEventTapOptionDefault,
    kCGHeadInsertEventTap,
    kCGMouseEventSubtype,
    kCGScrollWheelEventDeltaAxis1,
    kCGScrollWheelEventScrollPhase,
    kCGSessionEventTap,
)

from lib import volume
from lib.netflix import remote

# kCGEventTapDisabledByTimeout / kCGEventTapDisabledByUserInput
_TAP_DISABLED = (0xFFFFFFFE, 0xFFFFFFFF, -2, -1)
_MOMENTUM_PHASE_FIELD = 123  # kCGScrollWheelEventMomentumPhase

# kCGMouseEventSubtype identifies the pointing device: a physical mouse reports
# NX_SUBTYPE_DEFAULT (0); the trackpad reports NX_SUBTYPE_MOUSE_TOUCH (3). We only
# remap the physical mouse, so the trackpad (and anything else) keeps clicking.
_SUBTYPE_PHYSICAL_MOUSE = 0
_MOUSE_BUTTON_EVENTS = (
    int(kCGEventLeftMouseDown),
    int(kCGEventLeftMouseUp),
    int(kCGEventLeftMouseDragged),
    int(kCGEventRightMouseDown),
    int(kCGEventRightMouseUp),
    int(kCGEventRightMouseDragged),
)

# --- volume sender (lib/volume.py, asyncio) ---

# Volume is driven from a signed scroll ACCUMULATOR, not one send per scroll
# event. High-resolution / free-scrolling mice emit a torrent of large-delta
# events (measured: ~140 events/s, delta 8-17 each). Sending a volume step per
# event floods the Apple TV at ~20 steps/s; its HDMI-CEC volume pipeline can't
# apply them that fast, so it buffers and the volume keeps drifting ("inertia")
# after the wheel stops. Instead the tap thread just adds delta into _accum; the
# sender polls it and emits at most ONE step per the active target's gap in the
# net direction, consuming (zeroing) _accum on each send. So the rate is bounded
# and nothing carries over — the volume stops within one gap of the wheel
# stopping. The gap belongs to the target (lib/volume.py: the Apple TV's CEC
# pipeline is the slow one; the TV applies its own keys twice as fast), so it is
# read per step rather than fixed here. The first step still fires immediately
# (last_send=0), so a quick nudge stays responsive; only the ramp is paced.
_POLL = 0.02          # how often the sender samples the accumulator (low = snappy first step)
_DEADZONE = 2         # ignore net scroll smaller than this within a sample
_accum = 0            # signed scroll delta awaiting send (guarded by _accum_lock)
_accum_lock = threading.Lock()
_run_loop = None
_tap = None


def _msnow():
    """Wall-clock HH:MM:SS.mmm for correlating input arrivals with sends."""
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def _take_step():
    """Consume the accumulator if it's past the deadzone, returning +1/-1/0.

    Zeroing _accum on consume is what makes the volume stop with the wheel: once
    scroll events stop arriving, the leftover fires once and then there's nothing
    left, so no drift. Magnitude beyond direction is intentionally discarded —
    one step per send keeps a fast flick from blasting the volume."""
    global _accum
    with _accum_lock:
        acc = _accum
        if abs(acc) < _DEADZONE:
            return 0
        _accum = 0
    return 1 if acc > 0 else -1


async def _sender_main():
    """Sample the scroll accumulator and drive the volume.

    Which device a step reaches, how a dead connection is noticed, and what happens
    when the Apple TV can't be reached at all are lib/volume.py's business — it
    holds the live connection, falls over to the TV inside the failing notch, and
    brings the Apple TV back when it returns. Here we only pace: at most one step
    per the active target's gap, in the net direction of the wheel.
    """
    loop = asyncio.get_running_loop()
    vol = volume.Volume()
    last_send = 0.0
    while True:
        await asyncio.sleep(_POLL)
        now = loop.time()
        if now - last_send < vol.gap:
            continue  # rate limit — bounds the step rate the target must keep up with
        step = _take_step()
        if step == 0:
            continue
        target = await vol.step(step)
        # Paced from when the key was *delivered*, not from when we decided to send it.
        # Normally the difference is the ~10ms of the send itself; it matters on the one
        # notch that discovers a target is gone, where the send takes seconds and a
        # start-to-start clock lets the next notch fire the instant this one returns
        # (measured: two keys 23ms apart on the notch that fell over to the TV).
        last_send = loop.time()
        if target is not None:
            label = "+" if step > 0 else "-"
            print(f"  vol {label} → {target.name}  {_msnow()}", flush=True)


def _sender_loop():
    """Background thread: runs the asyncio sender on its own event loop."""
    try:
        asyncio.run(_sender_main())
    except Exception as e:  # keep the thread's death visible in the log
        print(f"Sender loop exited: {e}", flush=True)


# --- Scroll event tap ---


def _button_loop():
    """Background thread: run the button actions, which the tap thread must not.

    Driving Netflix means AppleScript, and one round-trip is ~0.1s while the fullscreen path
    can take a second — orders of magnitude past what an event tap may spend in its callback
    before macOS disables it for timing out. So the callback only enqueues.
    """
    while True:
        action = _BUTTONS.get()
        try:
            label = _ACTIONS[action]()
        except Exception as e:  # a broken tab must never kill the button thread
            label = f"failed: {e}"
        print(f"  {action} → {label}  {_msnow()}", flush=True)


_ACTIONS = {"play/pause": remote.play_pause, "fullscreen": remote.fullscreen}
# One in flight plus one waiting. A second press while the first is still running is a real
# second press and is kept; a burst past that is impatience, and queueing it would only make
# the toggles undo each other seconds after the user stopped clicking.
_BUTTONS: "queue.Queue[str]" = queue.Queue(maxsize=1)


def _press(action):
    """Hand a button action to the worker, dropping it if the queue is already backed up."""
    try:
        _BUTTONS.put_nowait(action)
    except queue.Full:
        pass


def _handle_scroll(event):
    """Mouse wheel → volume (swallowed); trackpad scroll → passed through."""
    phase = CGEventGetIntegerValueField(event, kCGScrollWheelEventScrollPhase)
    momentum = CGEventGetIntegerValueField(event, _MOMENTUM_PHASE_FIELD)
    delta = CGEventGetIntegerValueField(event, kCGScrollWheelEventDeltaAxis1)
    if _DEBUG:
        print(
            f"  [scroll] phase={phase} momentum={momentum} d1={delta} "
            f"{time.strftime('%H:%M:%S')}",
            flush=True,
        )

    # Trackpad scrolls carry a phase or momentum; a plain mouse wheel has neither.
    if phase or momentum:
        return event  # leave trackpad scrolling untouched

    # Mouse wheel: just add the delta to the accumulator (cheap, non-blocking);
    # the sender thread samples it on a tick and drives the Apple TV. Then swallow
    # the event so the Mac doesn't also scroll.
    if delta:
        with _accum_lock:
            global _accum
            _accum += delta
    return None  # returning NULL from an active tap drops the event


def _handle_mouse_button(event_type, event):
    """Physical-mouse left button → Netflix play/pause, right button → Netflix fullscreen
    (both swallowed); trackpad click → normal."""
    subtype = CGEventGetIntegerValueField(event, kCGMouseEventSubtype)
    if _DEBUG:
        print(f"  [click] type={event_type} subtype={subtype} "
              f"{time.strftime('%H:%M:%S')}", flush=True)
    if subtype != _SUBTYPE_PHYSICAL_MOUSE:
        return event  # trackpad (or any non-mouse device): leave the click alone
    # Physical mouse: queue one action on press, then swallow the whole click
    # (down/up/drag) so the mouse acts purely as a remote — no UI click, no menu.
    if event_type == int(kCGEventLeftMouseDown):
        _press("play/pause")
    elif event_type == int(kCGEventRightMouseDown):
        _press("fullscreen")
    return None


def _event_callback(_proxy, event_type, event, _refcon):
    """CGEventTap callback. Only does fast work (enqueue a command / synthesize a
    key), never blocking I/O, so the tap can't be disabled by timeout. If macOS
    disables it anyway (user input), re-enable it here.
    """
    et = int(event_type)
    if et in _TAP_DISABLED:
        if _tap:
            CGEventTapEnable(_tap, True)
        return event
    if et == int(kCGEventScrollWheel):
        return _handle_scroll(event)
    if et in _MOUSE_BUTTON_EVENTS:
        return _handle_mouse_button(et, event)
    return event


def _cleanup(*_args):
    """Clean up on Ctrl+C. The sender thread is a daemon and its pyatv
    connection drops with the process, so just stop the run loop."""
    print("\nClosing...", flush=True)
    if _run_loop:
        CFRunLoopStop(_run_loop)


def _check_tap(_timer, _info):
    """Keep the event tap healthy.

    macOS disables a tap on timeout (the port stays valid → just re-enable),
    but it can also invalidate the tap entirely across sleep/wake or the login
    boundary at reboot. A dead tap leaves the process alive yet deaf to scrolls,
    so KeepAlive can't rescue it — re-enabling a dead port is a no-op. Exit and
    let launchd respawn us with a fresh tap instead.
    """
    if _tap and CFMachPortIsValid(_tap):
        CGEventTapEnable(_tap, True)
    else:
        print("Event tap invalid — exiting so launchd restarts with a fresh tap",
              flush=True)
        os._exit(1)


def main():
    global _run_loop, _tap

    signal.signal(signal.SIGINT, _cleanup)

    threading.Thread(target=_sender_loop, daemon=True).start()
    threading.Thread(target=_button_loop, daemon=True).start()

    _tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,  # active tap: callback may drop events
        CGEventMaskBit(kCGEventScrollWheel)
        | CGEventMaskBit(kCGEventLeftMouseDown)
        | CGEventMaskBit(kCGEventLeftMouseUp)
        | CGEventMaskBit(kCGEventLeftMouseDragged)
        | CGEventMaskBit(kCGEventRightMouseDown)
        | CGEventMaskBit(kCGEventRightMouseUp)
        | CGEventMaskBit(kCGEventRightMouseDragged),
        _event_callback,
        None,
    )
    if _tap is None:
        # Report what TCC actually thinks, and of whom, instead of asserting the
        # cause. The grant is bound to the wrapper app's code signature AND path,
        # so a rebuilt stub or a moved repo silently drops it. ctypes because
        # AXIsProcessTrusted lives in ApplicationServices, not pyobjc-quartz.
        import ctypes
        import ctypes.util

        _app_services = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        _app_services.AXIsProcessTrusted.restype = ctypes.c_bool

        print("Failed to create event tap — grant Accessibility permission in "
              "System Settings > Privacy & Security > Accessibility")
        print(f"  AXIsProcessTrusted: {_app_services.AXIsProcessTrusted()}")
        print(f"  running as: {os.path.realpath(sys.executable)}")
        print(f"  launchd job: {os.environ.get('XPC_SERVICE_NAME', '(none)')}")
        sys.exit(1)

    source = CFMachPortCreateRunLoopSource(None, _tap, 0)
    _run_loop = CFRunLoopGetCurrent()
    CFRunLoopAddSource(_run_loop, source, kCFRunLoopCommonModes)

    # Poll tap health every 10s: re-enable if macOS disabled it, restart if it
    # went invalid (e.g. after wake/reboot). Short interval = fast self-heal.
    timer = CFRunLoopTimerCreate(
        kCFAllocatorDefault,
        time.time() + 10, 10,  # first fire in 10s, repeat every 10s
        0, 0, _check_tap, None,
    )
    CFRunLoopAddTimer(_run_loop, timer, kCFRunLoopCommonModes)

    print("Listening for scroll/click events (Ctrl+C to stop)", flush=True)
    while True:
        CFRunLoopRun()
        # CFRunLoopRun returned: nothing is keeping the loop alive. Re-arm if the
        # tap is still healthy; otherwise bail so launchd hands us a fresh one.
        if _tap and CFMachPortIsValid(_tap):
            CGEventTapEnable(_tap, True)
            time.sleep(1)
        else:
            print("Run loop exited with no valid tap — exiting for restart",
                  flush=True)
            os._exit(1)


if __name__ == "__main__":
    main()
