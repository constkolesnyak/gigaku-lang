"""One gated trace log for every feature — /tmp/gigaku-anki.log.

The three old add-ons kept three loggers with two formats and two of them defaulted to
*on*, growing without bound ("a permanently growing log in /tmp to record a bug that is
fixed is just litter" — the old code's own words, now honoured: debug defaults to off and
the file is truncated when it outgrows a megabyte).
"""
import os
import time

LOG_PATH = "/tmp/gigaku-anki.log"
MAX_BYTES = 1_000_000


def log(message):
    """Write one trace line when `debug` is on in the add-on config. Never raises."""
    try:
        from . import conf

        if not conf.debug():
            return
        try:
            if os.path.getsize(LOG_PATH) > MAX_BYTES:
                os.truncate(LOG_PATH, 0)
        except OSError:
            pass
        stamp = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d}"
        with open(LOG_PATH, "a", encoding="utf8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:  # noqa: BLE001 — a logger must never be the thing that breaks
        pass
