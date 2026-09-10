"""The one check on what an export *says*, not how much of it there is.

Every other gate in `gigaku subs` reads Language Reactor's own labels, and on 2026-08-14 a
label turned out not to be a reading: two episodes exported as Japanese with `tm.name` still
saying German, because LR reverted to the title's default track in the gap between the last
verify and the export click. Pure logic, no browser — see lib/subs/language.py.
"""
from lib.subs import language

# Written here rather than taken from a rip: the check reads scripts, so any sentence in the
# right alphabet is as good a specimen as a real subtitle, and these can say what they are.
GERMAN = "Die Akademie ist halb zerstört, also haben wir jetzt Ferien. " * 6
JAPANESE = "これは日本語の文章です。ひらがな、カタカナ、そして漢字。" * 12
RUSSIAN = "Это русский текст, написанный кириллицей целиком. " * 6


def test_a_track_in_its_own_script_passes():
    assert language.mismatch(GERMAN, "German") is None
    assert language.mismatch(JAPANESE, "Japanese") is None
    assert language.mismatch(RUSSIAN, "Russian") is None


def test_the_failure_that_prompted_this_is_caught():
    """Japanese cues in a file the run calls German — the E11/E12 rip, exactly."""
    why = language.mismatch(JAPANESE, "German")
    assert why and "Japanese" in why and "German" in why


def test_it_catches_the_mirror_case_too():
    assert language.mismatch(GERMAN, "Japanese")
    assert language.mismatch(RUSSIAN, "German")


def test_a_language_whose_script_is_unknown_is_never_refused():
    """Refuse only what it is sure of — the same rule `_lang_of` follows on the vocab side."""
    assert language.mismatch(JAPANESE, "Klingon") is None
    assert language.mismatch(JAPANESE, "") is None
    assert language.mismatch(JAPANESE, None) is None


def test_too_little_text_says_nothing():
    """A handful of cues can be a title card or a name; only an episode is evidence."""
    assert language.mismatch("これは日本語です。", "German") is None
    assert len(JAPANESE) > language.MIN_LETTERS  # …but a real export is far past the floor


def test_the_track_name_is_read_out_of_lrs_own_wording():
    assert language.expected_scripts("ASR Pro German") == ("latin",)
    assert language.expected_scripts("Japanese [Original]") == ("kana", "han")
    assert language.expected_scripts("Chinese (Simplified)") == ("han",)
    assert language.expected_scripts("Serbian") == ("cyrillic", "latin")
    assert language.expected_scripts("Klingon") is None


def test_kana_name_japanese_where_han_alone_cannot():
    """Han script is shared; kana are Japanese's alone, so their presence decides."""
    assert language.describe(language.script_counts(JAPANESE)) == "Japanese"
    assert language.describe(language.script_counts(GERMAN)) == "Latin"
    assert language.describe({}) == "no letters"


def test_a_stray_word_of_the_other_script_is_not_a_mismatch():
    """German subtitles quote a Japanese name now and then, and an ASR track is full of
    Latin-lettered noise. The margin this rides on is the whole alphabet, not a few words."""
    assert language.mismatch(GERMAN + "シャドウ ガーデン", "German") is None
    assert language.mismatch(JAPANESE + " Shadow Garden Academy ", "Japanese") is None
