"""Samsung Tizen TV control — the scroll wheel's volume fallback.

The Apple TV is the wheel's target; this is where a step goes when that one can't be
reached, which is the same speaker either way (the Apple TV only relays volume to this TV
over HDMI-CEC). See `control` for the protocol and for how the TV is found without mDNS.
Public surface re-exported so callers keep `from lib import samsung`.
"""
from lib.samsung.control import (
    Remote,
    SamsungTVError,
    close,
    connect,
    describe,
    find_tv,
    volume_down,
    volume_up,
)

__all__ = [
    "connect",
    "close",
    "volume_up",
    "volume_down",
    "describe",
    "find_tv",
    "Remote",
    "SamsungTVError",
]
