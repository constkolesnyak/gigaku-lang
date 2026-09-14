"""Apple TV control over the network (pyatv Companion) — the target of the scroll-wheel
volume daemon. See `control` for how the device is discovered under the AP's client
isolation. Public surface re-exported so callers keep `from lib import appletv`.
"""
from lib.appletv.control import (
    AppleTVError,
    close,
    connect,
    volume_down,
    volume_up,
)

__all__ = ["connect", "close", "volume_up", "volume_down", "AppleTVError"]
