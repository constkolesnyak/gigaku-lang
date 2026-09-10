"""Title → filename logic shared by `subs` (Netflix rip) and `excel_to_srt` (manual srt).

Both produce the same ``<Title>/<Title> - S01E03 - Primary.srt`` layout, so both must
sanitize a title *identically* — otherwise a show ripped one way (a `:` becoming a space)
and converted the other (a `:` becoming a dash) land in two different directories, silently
splitting one season in half. One function here is what keeps them in step.
"""
import re

# Characters that can't (or shouldn't) go in a path component, collapsed to a single space.
# Covers the Windows-reserved set plus control chars, so a title is safe on any filesystem.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def sanitize(title: str) -> str:
    """A title going into a path — illegal characters out, never empty."""
    return _ILLEGAL.sub(" ", title).strip() or "Subtitles"


def srt_base(title: str, season: int | None, episode: int) -> str:
    """The base filename for one episode's SRT pair.

    ``'Dark', 2, 3`` → ``'Dark - S02E03'``; a film (``season`` 0 or None) → just the
    sanitized title, since there is exactly one export and no episode to number.
    """
    if not season:
        return sanitize(title)
    return f"{sanitize(title)} - S{season:02d}E{episode:02d}"
