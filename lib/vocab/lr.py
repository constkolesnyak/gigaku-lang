"""Read a Language Reactor "saved items" JSON export into Words.

Export it at https://www.languagereactor.com/saved-items ▸ Export ▸ JSON ▸ All-All-Any-Any;
the file lands in ~/Downloads as lln_json_items_<date>_<n>.json.

polyglotka mirrored LR's entire item schema in 250 lines of pydantic models — every
transliteration, thumbnail and dependency-parse field — and then used exactly five of them.
This reads those five and ignores the rest, so a schema change on LR's side only breaks us
if it touches the fields we actually want.
"""
import json
from datetime import datetime

from lib.config import note
from lib.vocab.words import Stage, Word


def _word(item):
    # A saved item is a WORD or a PHRASE; phrases have no single lemma to track, and
    # polyglotka dropped them too.
    if item.get("itemType") != "WORD":
        return None
    stage = item.get("learningStage")
    if stage not in tuple(Stage):
        return None  # LR has never emitted another value; if it starts, ignore it quietly
    return Word(
        word=item["word"]["text"],
        language=item["langCode_G"].lower(),
        stage=Stage(stage),
        date=datetime.fromtimestamp(item["timeModified_ms"] / 1000),
    )


def read_exports(paths):
    words = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        found = [w for item in items if (w := _word(item))]
        words += found
        note(f'Read {len(found):,} words from "{path}".')
    return words
