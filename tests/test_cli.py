"""What the CLI does before it dispatches — the part that can take out a working feature."""
import sys

import pytest

from lib import cli
from lib.config import settings
from lib.subs import episode_range


def test_word_settings_do_not_gate_sound_and_subs(monkeypatch):
    """`sound` and `subs` read neither ~/Downloads nor ANKI_MIN_COUNTS. A stray
    GIGAKU_KNOWN_MORPHS_DIR in a shell profile must not cost the user their volume wheel."""
    monkeypatch.setattr(settings, "KNOWN_MORPHS_DIR", "/nope/not/a/directory")
    ran = []
    monkeypatch.setattr(cli, "_run", lambda args: ran.append(args.command))

    for command in ("sound", "subs"):
        monkeypatch.setattr("sys.argv", ["gigaku", command])
        cli.main()

    assert ran == ["sound", "subs"]


def test_word_commands_still_validate_their_settings(monkeypatch):
    monkeypatch.setattr(settings, "KNOWN_MORPHS_DIR", "/nope/not/a/directory")
    monkeypatch.setattr(cli, "_run", lambda args: None)
    monkeypatch.setattr("sys.argv", ["gigaku", "kanji"])

    with pytest.raises(SystemExit) as exc:  # UserError → exit 1, no traceback
        cli.main()
    assert exc.value.code == 1


def test_a_leading_dash_range_is_not_an_option(monkeypatch):
    """`gigaku subs -3` survives argparse only because no option string of the `subs`
    subparser looks like a negative number. Add a `-N`-shaped flag there and this dies as
    "unrecognized arguments" — silently, and only for the one form nobody tests by hand."""
    seen = []
    monkeypatch.setattr(cli, "_run", lambda args: seen.append(args.episodes))
    monkeypatch.setattr("sys.argv", ["gigaku", "subs", "-3"])

    cli.main()

    assert seen == ["-3"]


def test_a_track_name_shifts_out_of_the_range_slot():
    """argparse fills optional positionals left to right, so the README's
    `gigaku subs German "ASR Pro German"` lands the track name in `episodes`. Shape is what
    puts it back."""
    parse = cli._parser().parse_args

    assert cli._subs_args(parse(["subs"])) == (None, "German", "ASR Pro German")
    assert cli._subs_args(parse(["subs", "German", "ASR Pro German"])) == (
        None, "German", "ASR Pro German")
    assert cli._subs_args(parse(["subs", "3-12"])) == (
        episode_range.Range(3, 12, "3-12"), "German", "ASR Pro German")
    assert cli._subs_args(parse(["subs", "3-", "Korean"])) == (
        episode_range.Range(3, None, "3-"), "Korean", "ASR Pro German")


def test_a_bad_range_dies_before_the_browser_module_is_imported(monkeypatch, capsys):
    """A typo'd range must cost nothing — no pyobjc, no AppleScript, no Chrome."""
    monkeypatch.delitem(sys.modules, "lib.subs.subs", raising=False)
    monkeypatch.setattr("sys.argv", ["gigaku", "subs", "3--12"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "3-12" in capsys.readouterr().err          # the error teaches the forms
    assert "lib.subs.subs" not in sys.modules


def test_flags_beat_the_environment(monkeypatch):
    monkeypatch.setenv("GIGAKU_PLOTS_TITLE", "From env")
    monkeypatch.setattr(settings, "PLOTS_TITLE", "From env")
    monkeypatch.setattr(cli, "_run", lambda args: None)
    monkeypatch.setattr("sys.argv", ["gigaku", "plots", "--plots-title", "From flag"])

    cli.main()

    assert settings.PLOTS_TITLE == "From flag"


def test_no_translate_is_a_settings_flag_and_minus_three_still_parses():
    """`--no-translate` turns off the Claude gloss; adding it must not break `gigaku subs -3`,
    which reaches argparse as a positional only because no option here is `-N`-shaped."""
    args = cli._parser().parse_args(["subs", "-3", "--no-translate"])
    assert args.episodes == "-3" and args.subs_translate is False
    assert cli._parser().parse_args(["subs"]).subs_translate is None  # untouched → env/default


def test_no_spell_is_a_settings_flag_and_minus_three_still_parses():
    """`--no-spell` turns off the German proofreader. Same trap as `--no-translate`: it is not
    `-N`-shaped, so `gigaku subs -3` still reaches argparse as a positional."""
    args = cli._parser().parse_args(["subs", "-3", "--no-spell"])
    assert args.episodes == "-3" and args.subs_spell is False
    assert cli._parser().parse_args(["subs"]).subs_spell is None  # untouched → env/default


def test_a_typod_spell_range_is_answered_before_any_import(monkeypatch):
    """The shape of a range is the pure module's to judge, so a typo costs no AppKit import,
    no dictionary and no `claude` — the split `subs` and `translate` already make."""
    monkeypatch.setattr("sys.argv", ["gigaku", "spell", "--episodes", "3--12"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
