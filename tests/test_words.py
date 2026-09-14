"""The merge rule and the word queries — the logic the whole word side rests on."""
import json
import subprocess as _sp
from datetime import datetime

import pytest

from lib import config
from lib.vocab import words
from lib.config import UserError, settings
from lib.vocab.words import Stage, Word, merge


def w(text, lang="de", stage=Stage.KNOWN, day=1):
    return Word(text, lang, stage, datetime(2026, 1, day))


def test_latest_stage_wins():
    merged = merge([w("Haus", stage=Stage.LEARNING, day=1), w("Haus", stage=Stage.KNOWN, day=2)])
    assert [(x.word, x.stage) for x in merged] == [("Haus", Stage.KNOWN)]


def test_older_export_does_not_undo_a_newer_one():
    # Imports arrive in whatever order the files were globbed; only the timestamp decides.
    merged = merge([w("Haus", stage=Stage.KNOWN, day=2), w("Haus", stage=Stage.LEARNING, day=1)])
    assert merged[0].stage == Stage.KNOWN


def test_skipped_words_leave_the_store():
    merged = merge([w("Haus", stage=Stage.KNOWN, day=1), w("Haus", stage=Stage.SKIPPED, day=2)])
    assert merged == []


def test_skipped_then_relearned_comes_back():
    merged = merge([w("Haus", stage=Stage.SKIPPED, day=1), w("Haus", stage=Stage.LEARNING, day=2)])
    assert [x.stage for x in merged] == [Stage.LEARNING]


def test_same_spelling_in_two_languages_is_two_words():
    merged = merge([w("die", lang="de"), w("die", lang="en")])
    assert len(merged) == 2


def test_word_list_filters_by_language_and_stage():
    store = [w("Haus"), w("lernen", stage=Stage.LEARNING), w("chien", lang="fr")]
    assert words.word_list(store, "de") == ["Haus", "lernen"]
    assert words.word_list(store, "de", "known") == ["Haus"]
    assert words.word_list(store, "de", Stage.LEARNING) == ["lernen"]


def test_word_list_rejects_an_unknown_language():
    with pytest.raises(UserError, match="must be one of"):
        words.word_list([w("Haus")], "ja")


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "WORDS_CACHE", str(tmp_path / "words.json"))
    monkeypatch.setattr(config, "LEGACY_WORDS_CACHE", str(tmp_path / "nope.json"))

    store = [w("Haus"), w("犬", lang="ja", stage=Stage.LEARNING)]
    words.write_cache(store)
    assert words.read_cache() == store


def test_clear_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "WORDS_CACHE", str(tmp_path / "words.json"))
    monkeypatch.setattr(config, "CACHE_ADOPTED", str(tmp_path / ".adopted"))
    monkeypatch.setattr(config, "LEGACY_WORDS_CACHE", str(tmp_path / "nope.json"))

    words.write_cache([w("Haus")])
    words.clear_cache()
    assert words.read_cache() == []
    words.clear_cache()  # clearing an already-empty cache is not an error


def test_clear_cache_is_not_undone_by_the_legacy_adoption(tmp_path, monkeypatch):
    """polyglotka's cache file is never deleted, so keying the adoption on 'gigaku has no
    cache' alone would resurrect every cleared word on the very next read."""
    legacy = tmp_path / "polyglotka.json"
    legacy.write_text(
        json.dumps([{"word": "Haus", "language": "de", "learning_stage": "KNOWN",
                     "date": "2026-01-01T00:00:00"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "WORDS_CACHE", str(tmp_path / "cache" / "words.json"))
    monkeypatch.setattr(config, "CACHE_ADOPTED", str(tmp_path / "cache" / ".adopted"))
    monkeypatch.setattr(config, "LEGACY_WORDS_CACHE", str(legacy))

    assert words.read_cache() == [w("Haus")]  # adopted
    words.clear_cache()

    assert words.read_cache() == []  # and it stays cleared
    assert legacy.exists(), "clearing gigaku's cache must not touch polyglotka's file"


def test_status_messages_stay_off_stdout(tmp_path, monkeypatch, capsys):
    """`gigaku kanji > kanji.tsv` and `gigaku anki | pbcopy` send stdout on — chatter on it
    would land in the TSV / the clipboard."""
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "WORDS_CACHE", str(tmp_path / "words.json"))

    words.write_cache([w("Haus")])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Cached 1 words." in captured.err


def test_polyglotka_cache_is_adopted_once(tmp_path, monkeypatch):
    """Uninstalling polyglotka must not cost the user their history."""
    legacy = tmp_path / "polyglotka.json"
    legacy.write_text(
        json.dumps([{"word": "Haus", "language": "DE", "learning_stage": "KNOWN",
                     "date": "2026-01-01T00:00:00"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(config, "WORDS_CACHE", str(tmp_path / "cache" / "words.json"))
    monkeypatch.setattr(config, "CACHE_ADOPTED", str(tmp_path / "cache" / ".adopted"))
    monkeypatch.setattr(config, "LEGACY_WORDS_CACHE", str(legacy))

    assert words.read_cache() == [w("Haus")]  # language lowercased on the way in

    words.write_cache([])  # and once gigaku has a cache, the old one stops overwriting it
    assert words.read_cache() == []


# ── import_words: which commands are allowed to touch the user's files ────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A word store wired to tmp dirs, with one LR export sitting in 'Downloads'."""
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "WORDS_CACHE", str(tmp_path / "words.json"))
    monkeypatch.setattr(config, "CACHE_ADOPTED", str(tmp_path / ".adopted"))
    monkeypatch.setattr(config, "LEGACY_WORDS_CACHE", str(tmp_path / "nope.json"))
    monkeypatch.setattr(settings, "EXPORTED_FILES_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "KNOWN_MORPHS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "CHROME", False)
    monkeypatch.setattr(settings, "RM_PROCESSED_FILES", True)

    export = tmp_path / "lln_json_items_test.json"
    export.write_text(
        json.dumps([{"itemType": "WORD", "word": {"text": "Haus"}, "langCode_G": "de",
                     "learningStage": "KNOWN", "timeModified_ms": 1767225600000}]),
        encoding="utf-8",
    )
    return tmp_path, export


def test_migaku_silence_cannot_delete_a_word_another_source_calls_known():
    """Migaku's SKIPPED is UNKNOWN and IGNORED collapsed — 2,386 against 7, measured.

    So it is mostly "Migaku has met this word and you never marked it", which says nothing
    about Anki. Without `keep` it deleted 190 words AnkiMorphs was asserting, and a fresh
    crop every week as Anki runs ahead of Migaku.
    """
    anki = Word("十分", "ja", Stage.KNOWN, datetime(1, 1, 1))
    migaku_silence = Word("十分", "ja", Stage.SKIPPED, datetime(2026, 8, 1))

    assert merge([anki, migaku_silence]) == []  # the old behaviour, still the default
    kept = merge([anki, migaku_silence], keep={("十分", "ja")})
    assert [w.word for w in kept] == ["十分"]
    assert kept[0].stage == Stage.KNOWN  # …at its latest *tracked* state, not the SKIPPED one


def test_migaku_can_still_un_learn_its_own_words():
    # `keep` only shields what another source currently claims. A word nobody is asserting
    # still leaves the store when Migaku says so — that is the whole point of SKIPPED.
    old = Word("Haus", "de", Stage.KNOWN, datetime(2026, 1, 1))
    dropped = Word("Haus", "de", Stage.SKIPPED, datetime(2026, 8, 1))
    assert merge([old, dropped], keep={("十分", "ja")}) == []


def test_a_shielded_word_still_loses_to_a_newer_real_stage():
    # `keep` is not a freeze: it only disarms SKIPPED. LEARNING after KNOWN still wins.
    known = Word("犬", "ja", Stage.KNOWN, datetime(2026, 1, 1))
    later = Word("犬", "ja", Stage.LEARNING, datetime(2026, 8, 1))
    assert merge([known, later], keep={("犬", "ja")})[0].stage == Stage.LEARNING


def test_a_read_only_command_never_deletes_the_users_exports(store):
    """`gigaku words`/`kanji`/`plots` are reads. They pick up new words, but the export file
    the user may still want is not theirs to bin — only `gigaku import` consumes."""
    _, export = store

    found = words.import_words()  # what plots/kanji/words call

    assert [x.word for x in found] == ["Haus"]  # the new word is picked up…
    assert export.exists()  # …and the export survives


def test_import_consumes_the_exports_it_read(store):
    _, export = store

    words.import_words(cache_allowed=False, consume=True)  # what `gigaku import` calls

    assert not export.exists()


def test_known_morphs_skip_a_language_with_no_words_yet(store, monkeypatch):
    """A configured-but-not-yet-studied language is a Tuesday, not a crash — and crashing here
    aborted the import before it cleaned up the exports."""
    tmp, export = store
    monkeypatch.setattr(settings, "KNOWN_MORPHS_SAVE_LANGS", "de,ja")

    words.import_words(cache_allowed=False, consume=True)

    assert (tmp / "gigaku_known_morphs_de.csv").exists()
    assert not (tmp / "gigaku_known_morphs_ja.csv").exists()
    assert not export.exists()  # the import still finished


def test_a_broken_chrome_does_not_sink_a_command_with_a_good_cache(store, monkeypatch):
    """Not signed into Migaku, no Chrome, no IndexedDB — none of that should take out `plots`
    when the whole history is sitting in the cache."""
    tmp, export = store
    export.unlink()
    words.write_cache([w("Haus")])
    monkeypatch.setattr(settings, "CHROME", True)

    def boom():
        raise UserError("No Migaku data in any Chrome profile.")

    monkeypatch.setattr("lib.vocab.migaku.read_chrome", boom)

    assert words.import_words() == [w("Haus")]


def test_german_known_morphs_are_lemmatised_and_cached(tmp_path, monkeypatch):
    """Migaku's lowercase dictForms become the spaCy lemmas recalc matches (abend→Abend),
    and only uncached words pay the subprocess — a daily run costs yesterday's words."""
    import subprocess as sp
    from lib import config as cfg
    from lib.vocab import words as w_mod

    monkeypatch.setattr(cfg, "DE_LEMMA_CACHE", str(tmp_path / "de_lemmas.json"))
    monkeypatch.setattr(w_mod, "_spacy_python", lambda: "/fake/python")
    calls = []

    def fake_run(cmd, input=None, **kw):
        asked = json.loads(input)
        calls.append(asked)
        out = {word: word.capitalize() if word == "abend" else word for word in asked}
        return type("P", (), {"returncode": 0, "stdout": json.dumps(out), "stderr": ""})()

    monkeypatch.setattr(sp, "run", fake_run)
    # bypass the conftest stub — this test exercises the real function
    monkeypatch.setattr(w_mod, "_de_lemmas", w_mod._de_lemmas.__wrapped__
                        if hasattr(w_mod._de_lemmas, "__wrapped__") else w_mod._de_lemmas)
    real = w_mod._de_lemmas

    assert real(["abend", "gehen"]) == ["Abend", "gehen"]
    assert real(["abend", "gehen", "haus"]) == ["Abend", "gehen", "haus"]
    assert [sorted(c) for c in calls] == [["abend", "gehen"], ["haus"]]  # cache hit


def test_a_lemma_with_no_letters_falls_back_to_the_word(tmp_path, monkeypatch):
    """spaCy maps `akt` → "---" and `al` → "--" (12 such, measured). The literal became a
    CSV row and an AnkiMorphs known morph no script claims — noted on every backup — while
    the real word never got a row. A lemma with no letter is no lemma: keep the word."""
    import subprocess as sp
    from lib import config as cfg
    from lib.vocab import words as w_mod

    monkeypatch.setattr(cfg, "DE_LEMMA_CACHE", str(tmp_path / "de_lemmas.json"))
    monkeypatch.setattr(w_mod, "_spacy_python", lambda: "/fake/python")

    def fake_run(cmd, input=None, **kw):
        return type("P", (), {"returncode": 0, "stderr": "",
                              "stdout": json.dumps({"akt": "---", "abend": "Abend"})})()

    monkeypatch.setattr(sp, "run", fake_run)
    real = getattr(w_mod._de_lemmas, "__wrapped__", w_mod._de_lemmas)

    assert real(["akt", "abend"]) == ["Abend", "akt"]
    cached = json.loads((tmp_path / "de_lemmas.json").read_text(encoding="utf-8"))
    assert cached == {"akt": "akt", "abend": "Abend"}  # the junk never reaches the cache


def test_the_de_import_recognises_the_csvs_own_echo(store, monkeypatch):
    """The export writes `Alarmsyst` (a spaCy lemma), the import lowers it to `alarmsyst` —
    a form the cache never held, so it came back as a brand-new word at the year 1, daily
    (594 fabricated words in three days, measured 2026-08-08). The lemma cache is the record
    of what was exported; its lowered keys ∪ values ride along as already-known."""
    from lib import config as cfg
    from lib.vocab import ankimorphs

    tmp, _ = store
    (tmp / "de_lemmas.json").write_text(
        json.dumps({"alarmsystem": "Alarmsyst", "aboriginen": "Aborigin"}), encoding="utf-8")
    monkeypatch.setattr(cfg, "DE_LEMMA_CACHE", str(tmp / "de_lemmas.json"))
    monkeypatch.setattr(settings, "ANKIMORPHS_LANGS", "de")
    seen = {}

    def fake_read_known(known=None, **_kw):
        seen.update(known or {})
        return [], set()

    monkeypatch.setattr(ankimorphs, "read_known", fake_read_known)
    words.import_words()

    # Keys (the cache words) and values (the CSV rows), all lowered — the echo in both
    # generations of the aborigine chain is already-represented, not new.
    assert {"alarmsystem", "alarmsyst", "aboriginen", "aborigin"} <= seen["de"]


def test_a_missing_lemma_cache_adds_nothing(store, monkeypatch):
    from lib import config as cfg
    from lib.vocab import words as w_mod

    monkeypatch.setattr(cfg, "DE_LEMMA_CACHE", str(store[0] / "absent.json"))
    assert w_mod._de_exported_forms() == set()


@pytest.mark.parametrize("exc", [
    UserError("no AnkiMorphs spaCy venv"),
    FileNotFoundError("python vanished — Anki bumped its embedded python"),
    json.JSONDecodeError("garbled", "doc", 0),  # a ValueError
    _sp.TimeoutExpired(cmd="python", timeout=600),
])
def test_a_failed_de_normalisation_keeps_the_existing_csv(tmp_path, monkeypatch, exc):
    """Overwriting a good normalized CSV with a raw lowercase dump would silently unmark
    every noun at the next recalc — on failure the file must stand untouched. The whole
    failure set, not just UserError: the first uncaught OSError took the daily backup
    down with it instead of keeping any file."""
    from lib.vocab import words as w_mod

    monkeypatch.setattr(settings, "KNOWN_MORPHS_DIR", str(tmp_path))
    good = tmp_path / "gigaku_known_morphs_de.csv"
    good.write_text("Morph-Lemma\nAbend\n", encoding="utf-8")

    def boom(known):
        raise exc

    monkeypatch.setattr(w_mod, "_de_lemmas", boom)
    w_mod.save_known_morphs("de", [w("abend", lang="de")])

    assert good.read_text(encoding="utf-8") == "Morph-Lemma\nAbend\n"


def test_an_empty_known_list_never_overwrites_the_csv(tmp_path, monkeypatch):
    """A language left holding only LEARNING words used to write "Morph-Lemma\\n\\n" over
    the good file — recalc would then unmark every morph at the next R."""
    from lib.vocab import words as w_mod

    monkeypatch.setattr(settings, "KNOWN_MORPHS_DIR", str(tmp_path))
    good = tmp_path / "gigaku_known_morphs_de.csv"
    good.write_text("Morph-Lemma\nAbend\n", encoding="utf-8")

    w_mod.save_known_morphs("de", [w("lernen", stage=Stage.LEARNING)])

    assert good.read_text(encoding="utf-8") == "Morph-Lemma\nAbend\n"


def test_a_collapsed_known_list_refuses_to_shrink_the_csv(tmp_path, monkeypatch):
    """The repo's _gate fires only after Anki's copy is already overwritten — the file
    recalc reads needs its own gate. A modest shrink (un-learned words) still writes."""
    from lib.vocab import words as w_mod

    monkeypatch.setattr(settings, "KNOWN_MORPHS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "BACKUP_SHRINK_PCT", 10)
    path = tmp_path / "gigaku_known_morphs_ja.csv"
    path.write_text("Morph-Lemma\n" + "\n".join(f"w{i}" for i in range(100)) + "\n",
                    encoding="utf-8")

    w_mod.save_known_morphs("ja", [w(f"w{i}", lang="ja") for i in range(50)])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 101  # refused

    w_mod.save_known_morphs("ja", [w(f"w{i}", lang="ja") for i in range(95)])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 96  # a real quiet shrink


def test_the_freshest_spacy_venv_wins_and_a_dangling_symlink_is_no_venv(tmp_path, monkeypatch):
    """"3_13" < "3_9" as strings — a lexicographic pick hands a stale venv the job. And an
    Anki python bump leaves the interpreter a dangling symlink: glob still returns it, so
    "the venv is there" must mean "its interpreter runs"."""
    import glob as glob_mod
    from lib.vocab import words as w_mod

    real = []
    for ver in ("3_9", "3_13"):
        p = tmp_path / f"spacy-venv-python-{ver}" / "bin"
        p.mkdir(parents=True)
        (p / "python").write_text("", encoding="utf-8")
        real.append(str(p / "python"))
    broken = tmp_path / "spacy-venv-python-3_14" / "bin"
    broken.mkdir(parents=True)
    (broken / "python").symlink_to(tmp_path / "gone")
    monkeypatch.setattr(glob_mod, "glob", lambda pat: real + [str(broken / "python")])

    assert w_mod._spacy_python() == str(tmp_path / "spacy-venv-python-3_13" / "bin" / "python")
