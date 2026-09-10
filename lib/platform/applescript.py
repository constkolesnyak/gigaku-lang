"""NSAppleScript wrapper — replaces all subprocess osascript calls."""

import time

import Foundation

# Apple-event errors that are transient (Chrome momentarily busy / not answering), not real
# failures — retry these a few times instead of letting them kill a long subs run.
#   -1712 = AppleEvent timed out   -609/-600 = connection invalid / app launching
_TRANSIENT = {-1712, -609, -600}


class AppleScriptError(Exception):
    """Raised when an AppleScript fails to execute."""

    def __init__(self, message: str, error_number: int | None = None):
        super().__init__(message)
        self.error_number = error_number


def _once(source: str) -> tuple[str | None, AppleScriptError | None]:
    script = Foundation.NSAppleScript.alloc().initWithSource_(source)
    result, error = script.executeAndReturnError_(None)
    if error is not None:
        number = error.get("NSAppleScriptErrorNumber")
        message = error.get("NSAppleScriptErrorBriefMessage", str(error))
        return None, AppleScriptError(message, error_number=number)
    return (result.stringValue() if result is not None else None), None


def run(source: str, *, retries: int = 3) -> str | None:
    """Execute AppleScript and return the string result, or None if no result.

    Transient Apple-event errors (a momentarily busy/unresponsive Chrome → "AppleEvent timed
    out") are retried a few times rather than raised — one such hiccup used to crash the whole
    subs run. Real errors (script errors, app not scriptable, JS-from-Apple-Events off) raise
    immediately.
    """
    for attempt in range(1, retries + 1):
        result, error = _once(source)
        if error is None:
            return result
        if error.error_number not in _TRANSIENT or attempt == retries:
            raise error
        time.sleep(0.8 * attempt)
