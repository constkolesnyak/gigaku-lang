"""Make the Anki add-on package importable: `from gigaku import rules`.

The add-on's pure modules (anki/gigaku/{rules,queries,trim_rules}.py) import no aqt — the
package's __init__ guards it — so pytest imports them directly and pins them against their
lib/anki mirrors. This path insertion is the whole trick.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "anki"))


@pytest.fixture(autouse=True)
def _no_live_ankimorphs(monkeypatch):
    """Keep `import_words()` away from the user's running Anki.

    Unlike every other source, AnkiMorphs has no export file to find — it asks the live
    collection every time — so a test that merely calls `import_words` would read the real
    one and assert against whatever the user happens to know today. `lib/vocab/ankimorphs.py`
    is tested directly, against fixtures.
    """
    from lib.config import settings

    monkeypatch.setattr(settings, "ANKIMORPHS_LANGS", "")


@pytest.fixture(autouse=True)
def _no_live_spacy(monkeypatch):
    """No test may subprocess into the live AnkiMorphs spaCy venv.

    `save_known_morphs("de", …)` lemmatises through it; a test exercising the write path
    would depend on a 600-MB venv existing on the machine and pay seconds per run. The
    stub keeps the shape (sorted, deduplicated) without the model; the real normalisation
    has its own unit test with a stubbed subprocess.

    Beware what the identity transform HIDES: the real lemmatiser returns forms that are
    not the input (abend → Abend), which is exactly what fed the export→import round trip
    that fabricated 594 words (2026-08-08). Any test of that seam must monkeypatch a
    NON-identity fake — test_words' echo test does — or it passes by construction.
    """
    from lib.vocab import words

    original = words._de_lemmas

    def stub(known):
        return sorted(set(known))

    stub.__wrapped__ = original  # the normalisation unit test reaches the real one here
    monkeypatch.setattr(words, "_de_lemmas", stub)


@pytest.fixture(autouse=True)
def _no_live_claude(monkeypatch):
    """No test may spend a real `claude -p` call.

    Both callers (`gigaku clarity`, the subtitle translator) reach the subscription through
    `lib.claude.ask`, and a call is minutes and real money — a test that slipped past its
    own stub would bill the user rather than fail. Stubbing the one transport makes that
    impossible; a test that means to exercise a reply monkeypatches over this.
    """
    from lib import claude

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to call `claude` for real — stub lib.claude.ask")

    monkeypatch.setattr(claude, "ask", refuse)
