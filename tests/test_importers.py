"""Language Reactor JSON and Migaku CSV, read against real export fixtures."""
import json
from datetime import datetime
from pathlib import Path

from lib.vocab import lr, migaku
from lib.vocab.words import Stage, merge

DATA = Path(__file__).parent / "data"


def test_migaku_csv_maps_its_states_onto_ours():
    words = migaku.read_exports([DATA / "migaku_words_de.csv"])

    assert len(words) > 1000
    assert {w.language for w in words} == {"de"}
    assert {w.stage for w in words} <= {Stage.KNOWN, Stage.LEARNING, Stage.SKIPPED}
    assert any(w.stage == Stage.KNOWN for w in words)
    assert all(w.date.year >= 2020 for w in words)  # 'mod' is epoch *milliseconds*


def test_migaku_ignored_words_never_reach_the_store():
    words = migaku.read_exports([DATA / "migaku_words_ja.csv"])
    kept = merge(words)
    assert all(w.stage in (Stage.KNOWN, Stage.LEARNING) for w in kept)
    assert len(kept) <= len(words)


def test_migaku_unknown_state_is_skipped_not_guessed():
    assert migaku._word({"dictForm": "x", "mod": "0", "language": "de", "knownStatus": "FUTURE"}) is None


def test_lr_json_reads_words_and_ignores_phrases(tmp_path):
    export = tmp_path / "lln_json_items_test.json"
    export.write_text(
        json.dumps([
            {
                "itemType": "WORD",
                "word": {"text": "Haus", "translit": None},
                "langCode_G": "DE",
                "learningStage": "KNOWN",
                "timeModified_ms": 1767225600000,
            },
            {"itemType": "PHRASE", "langCode_G": "de", "learningStage": "KNOWN",
             "timeModified_ms": 1767225600000},
        ]),
        encoding="utf-8",
    )

    words = lr.read_exports([export])
    assert len(words) == 1
    assert words[0].word == "Haus"
    assert words[0].language == "de"
    assert words[0].stage == Stage.KNOWN
    assert words[0].date == datetime.fromtimestamp(1767225600)
