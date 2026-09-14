"""What language an export is *actually* in — the receipt Language Reactor's labels can't give.

Every gate in `gigaku subs` reads LR's own metadata: ``tm.type``/``tm.name`` for the subtitle
track, ``am.name`` for the audio. That is a label, and on 2026-08-14 a whole season proved a
label is not a reading. The Eminence in Shadow E11 and E12 exported as **Japanese** while every
check passed — measured live afterwards on the same tab: the player's audio really was German
(``getAudioTrack() → German``), LR's ``am`` really said German, and LR's loaded text track was
``closedcaptions:Japanese`` holding 392 cues — the exact 392 that landed in E12's Primary. LR
reverts a set track on its own clock (see ``_TrackWatch``), the export happens after a rewind
that makes it rebuild, and the revert landed in that gap. `_verify_tracks` had already run and
nothing looked again, so the run wrote Japanese into a German file and reported ``✓``.

A script check is what closes that, because it is the one thing the wrong track cannot fake: a
German subtitle file is Latin from end to end and Japanese ASR is kana and Han from end to end,
so the margin is the whole alphabet rather than a threshold to tune.

It refuses only what it is **sure** of, the same rule ``lib/vocab/ankimorphs.py::_lang_of``
follows: a language whose script this doesn't know, or a sample too short to read, is passed
without comment. Never guess a language — either the text is overwhelmingly the wrong script or
this says nothing.

Pure stdlib and pure logic, like ``naming`` and ``episode_range``, so the tests reach it without
pyobjc and without a browser.
"""

# Character ranges per script. Only *letters* are counted: punctuation, digits, spaces and the
# LRM marks LR sprinkles through its exports say nothing about the language and would dilute
# every share towards the middle.
_RANGES: tuple[tuple[str, int, int], ...] = (
    ("latin", 0x41, 0x5A), ("latin", 0x61, 0x7A),
    ("latin", 0xC0, 0x24F),          # Latin-1 letters + Extended-A/B: ä ö ü ß é ñ …
    ("latin", 0x1E00, 0x1EFF),       # Latin Extended Additional (Vietnamese)
    ("greek", 0x370, 0x3FF), ("greek", 0x1F00, 0x1FFF),
    ("cyrillic", 0x400, 0x52F),
    ("hebrew", 0x590, 0x5FF),
    ("arabic", 0x600, 0x6FF),
    ("devanagari", 0x900, 0x97F),
    ("thai", 0xE00, 0xE7F),
    ("hangul", 0x1100, 0x11FF), ("hangul", 0x3130, 0x318F), ("hangul", 0xAC00, 0xD7AF),
    ("kana", 0x3040, 0x30FF), ("kana", 0x31F0, 0x31FF), ("kana", 0xFF66, 0xFF9F),
    ("han", 0x3400, 0x4DBF), ("han", 0x4E00, 0x9FFF), ("han", 0xF900, 0xFAFF),
)

# The scripts a language is allowed to be written in. Keys are matched as substrings of a track
# name, so "Japanese [Original]", "ASR Pro German" and "Chinese (Simplified)" all land. A name
# no key claims is not checked at all — see the module docstring.
LANGUAGE_SCRIPTS: dict[str, tuple[str, ...]] = {
    "japanese": ("kana", "han"),
    "chinese": ("han",),
    "cantonese": ("han",),
    "korean": ("hangul",),
    "russian": ("cyrillic",),
    "ukrainian": ("cyrillic",),
    "bulgarian": ("cyrillic",),
    "greek": ("greek",),
    "hebrew": ("hebrew",),
    "arabic": ("arabic",),
    "hindi": ("devanagari",),
    "thai": ("thai",),
    # Serbian is written in both alphabets, and Netflix labels neither — so it accepts both
    # rather than rejecting half its own tracks.
    "serbian": ("cyrillic", "latin"),
}
for _latin in (
    "german", "english", "french", "spanish", "portuguese", "italian", "dutch", "polish",
    "czech", "slovak", "slovenian", "croatian", "romanian", "hungarian", "turkish", "swedish",
    "norwegian", "danish", "finnish", "icelandic", "indonesian", "malay", "vietnamese",
    "filipino", "tagalog", "catalan", "basque", "galician", "afrikaans", "swahili", "latin",
):
    LANGUAGE_SCRIPTS[_latin] = ("latin",)

# What to call a script when reporting what arrived instead.
_SCRIPT_NAMES = {
    "latin": "Latin", "cyrillic": "Cyrillic", "greek": "Greek", "hebrew": "Hebrew",
    "arabic": "Arabic", "devanagari": "Devanagari", "thai": "Thai", "hangul": "Korean",
    "kana": "Japanese", "han": "Chinese or Japanese",
}

# Below this many letters the sample says nothing — a handful of cues can be a title card, a
# name, or a line of song. An episode's export is thousands of letters, so this only ever
# silences the degenerate cases.
MIN_LETTERS = 200

# The share of letters that must be in one of the language's own scripts. Deliberately far from
# both edges: a real German track measures ~1.00 here and the Japanese track that prompted this
# measured ~0.01, so anything in this region is a decision the check should not be making alone.
MIN_SHARE = 0.30


def script_counts(text: str) -> dict[str, int]:
    """How many letters of each script ``text`` holds. Unclassified characters are ignored."""
    counts: dict[str, int] = {}
    for ch in text:
        cp = ord(ch)
        for script, lo, hi in _RANGES:
            if lo <= cp <= hi:
                counts[script] = counts.get(script, 0) + 1
                break
    return counts


def expected_scripts(language: str | None) -> tuple[str, ...] | None:
    """The scripts a track named ``language`` may be written in, or None if we don't know.

    Matched longest key first, so a name carrying two of them ("Chinese" inside "Traditional
    Chinese") can't be decided by dictionary order.
    """
    name = (language or "").lower()
    if not name:
        return None
    for key in sorted(LANGUAGE_SCRIPTS, key=len, reverse=True):
        if key in name:
            return LANGUAGE_SCRIPTS[key]
    return None


def describe(counts: dict[str, int]) -> str:
    """Name the language a letter profile points at — Japanese whenever kana are present,
    since kana are Japanese's alone and Han script by itself cannot tell the two apart."""
    if not counts:
        return "no letters"
    if counts.get("kana"):
        return "Japanese"
    dominant = max(counts, key=lambda s: counts[s])
    return _SCRIPT_NAMES.get(dominant, dominant)


def mismatch(text: str, language: str | None) -> str | None:
    """``None`` if ``text`` can be ``language``; otherwise why it plainly can't.

    The returned string is a whole reason, ready to be handed to a caller's error message.
    """
    want = expected_scripts(language)
    if not want:
        return None                      # a language whose script we don't know — say nothing
    counts = script_counts(text)
    total = sum(counts.values())
    if total < MIN_LETTERS:
        return None                      # too little text to read anything into
    share = sum(counts.get(s, 0) for s in want) / total
    if share >= MIN_SHARE:
        return None
    return (f"the text is {describe(counts)}, not {language} — "
            f"{share:.0%} of {total} letters are {'/'.join(want)}")
