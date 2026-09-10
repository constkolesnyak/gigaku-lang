"""The drift alarm: the same fact must read the same everywhere it is written down.

The German notetype name lives in four places (gigaku's DEFAULTS twice, its config.json,
MvJ's MediaConfig, the CLI's clarity profile) and the study decks in three — a rename in
any one silently desyncs the others (K opens nothing, recalc scoping misses, clarity
scores the wrong notetype). One cheap test instead of a config refactor: drift fails
here, with the two disagreeing values named.

gigaku.core.conf imports aqt at module level, so a minimal stub goes in first — the same
trick MvJ's own tests use. MvJ's config.py is read as SOURCE (its import chain is the
whole add-on); the dataclass defaults are regexed out. The add-on itself is third-party and
not in this repository — it is symlinked in at anki/MvJ Japanese (gitignored) where it is
installed, and the checks that read it skip where it is not.
"""
import json
import re
import sys
import types
from pathlib import Path

import pytest

if "aqt" not in sys.modules:  # gigaku.core.conf does `from aqt import mw`
    _aqt = types.ModuleType("aqt")
    _aqt.mw = None
    sys.modules["aqt"] = _aqt

from gigaku.core import conf  # noqa: E402

from lib import config as cli_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MVJ_CONFIG = ROOT / "anki" / "MvJ Japanese" / "mvj" / "config.py"
needs_mvj = pytest.mark.skipif(not MVJ_CONFIG.exists(),
                               reason="the MvJ add-on is not checked out beside anki/gigaku")


def mvj_default(field_name):
    src = MVJ_CONFIG.read_text(encoding="utf-8")
    m = re.search(rf'^\s+{field_name}: str = "([^"]+)"', src, re.MULTILINE)
    assert m, f"MediaConfig.{field_name} not found in mvj/config.py"
    return m.group(1)


def test_defaults_match_config_json():
    # config.json is what Anki ships as addonConfigDefaults — the GigakuTab reads
    # through it, so a section living only in conf.DEFAULTS would silently lose its
    # rows (am_toolbar was exactly that).
    on_disk = json.loads((ROOT / "anki" / "gigaku" / "config.json").read_text("utf-8"))
    assert conf.DEFAULTS == on_disk


def test_the_german_notetype_is_one_name_everywhere():
    name = conf.DEFAULTS["nav"]["de_notetype"]
    assert conf.DEFAULTS["counter"]["notetypes"][1] == name
    assert cli_config.clarity_profile("de").notetype == name


@needs_mvj
def test_mvj_agrees_on_the_german_notetype():
    assert mvj_default("german_notetype") == conf.DEFAULTS["nav"]["de_notetype"]


def test_the_japanese_notetype_is_one_name_everywhere():
    name = conf.DEFAULTS["counter"]["notetype"]
    assert conf.DEFAULTS["counter"]["notetypes"][0] == name
    assert cli_config.CLARITY_NOTETYPE == name


def test_the_study_decks_are_one_name_everywhere():
    # gigaku reads MvJ's MediaConfig live (study_deck_for) — these are the FALLBACKS,
    # and the CLI's hardcodes; all three answers to one question must agree.
    conf_src = (ROOT / "anki" / "gigaku" / "core" / "conf.py").read_text("utf-8")
    assert cli_config.CLARITY_STUDY_DECK == "Study 🇯🇵"
    assert '"Study 🇯🇵"' in conf_src
    assert cli_config.clarity_profile("de").study_deck == "Study 🇩🇪"
    assert '"Study 🇩🇪"' in conf_src


@needs_mvj
def test_mvj_agrees_on_the_study_decks():
    assert mvj_default("study_deck") == cli_config.CLARITY_STUDY_DECK
    assert mvj_default("study_deck_de") == cli_config.clarity_profile("de").study_deck
