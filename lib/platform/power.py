"""Keep the display awake via an IOKit power assertion — no subprocess / caffeinate.

Netflix reports *every* tab ``document.hidden`` when the display sleeps, which stalls
Language Reactor's ASR subtitle generation (and defeats bringing the tab to the
foreground). Holding a ``PreventUserIdleDisplaySleep`` assertion for the duration of a
batch export keeps the screen on so the Netflix tab stays visible.

Pure ctypes against IOKit/CoreFoundation, consistent with the project's no-subprocess
design (osascript→NSAppleScript, system_profiler→CoreGraphics).
"""
import ctypes
import ctypes.util

_iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFRelease.argtypes = [ctypes.c_void_p]

_iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int
_iokit.IOPMAssertionCreateWithName.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
]
_iokit.IOPMAssertionRelease.restype = ctypes.c_int
_iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]

_UTF8 = 0x08000100
_LEVEL_ON = 255


def _cfstr(s: str) -> ctypes.c_void_p:
    return _cf.CFStringCreateWithCString(None, s.encode("utf-8"), _UTF8)


class DisplayWakeLock:
    """Context manager that keeps the display awake while held.

    Usage::

        with DisplayWakeLock("gigaku export"):
            ...  # display stays on

    ``.active`` reports whether the assertion was actually created.
    """

    def __init__(self, reason: str = "gigaku"):
        self.reason = reason
        self._id: int | None = None

    def __enter__(self) -> "DisplayWakeLock":
        aid = ctypes.c_uint32(0)
        atype = _cfstr("PreventUserIdleDisplaySleep")
        name = _cfstr(self.reason)
        rc = _iokit.IOPMAssertionCreateWithName(atype, _LEVEL_ON, name, ctypes.byref(aid))
        _cf.CFRelease(atype)
        _cf.CFRelease(name)
        self._id = aid.value if rc == 0 else None
        return self

    def __exit__(self, *exc) -> None:
        if self._id is not None:
            _iokit.IOPMAssertionRelease(ctypes.c_uint32(self._id))
            self._id = None

    @property
    def active(self) -> bool:
        return self._id is not None


if __name__ == "__main__":
    import time

    with DisplayWakeLock("gigaku power test") as lock:
        print("assertion active:", lock.active)
        print("holding for 3s — check: pmset -g assertions | grep -i display")
        time.sleep(3)
    print("released")
