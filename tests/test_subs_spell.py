"""The German proofreader: the reply contract, the guards, and the record that undoes a pass.

Pure — `lib.claude.ask` is stubbed everywhere (tests/conftest.py refuses a real call) and
macOS's dictionary is stubbed wherever it would be reached, so nothing here spends a
subscription request or depends on a spelling dictionary being installed.

What is pinned is the part that decides whether a corrected Primary is trustworthy. This pass
rewrites the one German file the user has, and its dangerous failure is not a missed typo but
an invented sentence or a corrupted name — so most of what follows is about what the model is
structurally unable to write into the file.
"""

import pytest

from lib.subs import spell, spell_prompt, srt


def cue(index, text, start="00:00:01,000"):
    return srt.Cue(index, f"{start} --> 00:00:02,000", text)


def view(*cues):
    return [(c, True) for c in cues]


# ── the reply contract ───────────────────────────────────────────────────────

def test_parse_matches_on_the_echoed_id_not_on_position():
    v = view(cue(7, "Die Göttinn schwieg."), cue(8, "Er ging."))
    changes, _ = spell_prompt.parse("8: OK\n7: Die Göttin schwieg.", v)
    assert changes == {7: "Die Göttin schwieg."}


def test_parse_ignores_an_id_that_was_never_asked_for():
    changes, _ = spell_prompt.parse("99: Etwas ganz anderes.", view(cue(7, "Er ging.")))
    assert changes == {}


def test_silence_is_a_keep_not_a_gap():
    """The deliberate divergence from the glosser: a line nobody answered simply stands as it
    was ripped, which is the state the file was already in."""
    v = view(cue(1, "Er ging."), cue(2, "Sie blieb."))
    changes, _ = spell_prompt.parse("1: OK", v)
    assert changes == {}
    assert spell_prompt.unanswered("1: OK", v) == [2]


def test_ok_and_an_unchanged_line_both_produce_no_entry():
    v = view(cue(1, "Er ging."), cue(2, "Sie blieb."))
    changes, _ = spell_prompt.parse("1: OK\n2: Sie blieb.", v)
    assert changes == {}


def test_parse_keeps_the_first_answer_for_a_repeated_id():
    v = view(cue(1, "Die Göttinn schwieg."))
    changes, _ = spell_prompt.parse(
        "1: Die Göttin schwieg.\n1: Die Göttin sprach doch.", v)
    assert changes == {1: "Die Göttin schwieg."}


def test_a_bare_id_does_not_eat_the_next_answer():
    """`\\s` instead of `[ \\t]` in the pattern would parse this as id 1 answering with
    "2: Die Göttin schwieg." — the failure the echoed id exists to make impossible."""
    v = view(cue(1, "Er ging."), cue(2, "Die Göttinn schwieg."))
    changes, _ = spell_prompt.parse("1:\n2: Die Göttin schwieg.", v)
    assert changes == {2: "Die Göttin schwieg."}


def test_an_answer_wrapped_in_quotes_is_unwrapped_not_refused():
    v = view(cue(1, "Die Göttinn schwieg."))
    changes, refused = spell_prompt.parse('1: "Die Göttin schwieg."', v)
    assert changes == {1: "Die Göttin schwieg."} and not refused


# ── the guards ───────────────────────────────────────────────────────────────

def test_a_misspelling_is_accepted():
    assert spell_prompt.refuse("Die Göttinn gab ihm ihren Segen.",
                               "Die Göttin gab ihm ihren Segen.") is None


def test_a_respelled_name_is_accepted_however_short_the_line():
    """The first version of this budgeted the whole line's letters at ``len // 8``, which on a
    three-word line is one edit — and `Esteth → Esdeath` is two. Respelling a name is the
    commonest correction in these files, so a line-length budget refused it exactly where it
    was most obviously right."""
    assert spell_prompt.refuse("Esteth kam allein.", "Esdeath kam allein.") is None


def test_a_capitalisation_fix_is_still_an_edit():
    assert spell_prompt.refuse("ankunft in Berlin", "Ankunft in Berlin") is None
    changes, _ = spell_prompt.parse("1: Ankunft in Berlin", view(cue(1, "ankunft in Berlin")))
    assert changes == {1: "Ankunft in Berlin"}


def test_a_word_split_and_a_word_joined_are_accepted():
    assert spell_prompt.refuse("Ihmeine Chance!", "Ihm eine Chance!") is None


def test_a_paraphrase_is_refused():
    assert spell_prompt.refuse("Die Göttinn gab ihm ihren Segen.",
                               "Die Göttin nahm ihm seinen Fluch.")


def test_a_word_added_out_of_nowhere_is_refused():
    assert spell_prompt.refuse("Ich gehe.", "Ich gehe nicht.")


def test_a_different_word_is_refused_even_when_it_is_one_word():
    assert spell_prompt.refuse("Incursio ist bereit.", "Inquisition ist bereit.")


def test_more_than_one_line_is_refused():
    assert spell_prompt.refuse("Er ging.", "Er ging.\nSie blieb.")


def test_a_settled_name_may_not_be_respelled():
    """macOS's own dictionary wants Midgar → Midgard, and a model reaching for the nearest
    German word makes the same mistake. Once the series has settled the name, it cannot."""
    assert spell_prompt.refuse("Der Weg nach Midgar.", "Der Weg nach Midgard.", {"Midgar"})
    assert spell_prompt.refuse("Der Weg nach Midgar.", "Der Weg nach Midgard.") is None


def test_the_ledger_only_protects_it_never_blocks_a_fix_towards_it():
    assert spell_prompt.refuse("Esteth kam allein.", "Esdeath kam allein.", {"Esdeath"}) is None


def test_a_refusal_is_reported_not_swallowed():
    _, refused = spell_prompt.parse("1: Die Göttin nahm ihm seinen Fluch.",
                                    view(cue(1, "Die Göttinn gab ihm ihren Segen.")))
    assert len(refused) == 1 and refused[0]["cue"] == 1 and refused[0]["why"]


# ── the request ──────────────────────────────────────────────────────────────

def test_context_lines_carry_no_id():
    text = spell_prompt.render([(cue(1, "Vorher"), False), (cue(2, "Jetzt"), True)])
    assert "1:" not in text and "2: Jetzt" in text


def test_the_request_states_how_many_lines_it_wants():
    assert "Proofread the 2 lines" in spell_prompt.render(view(cue(1, "A"), cue(2, "B")))


def test_the_dictionary_is_listed_never_marked_inside_the_german():
    """A marker inside the text is a character the model can echo back into the file."""
    text = spell_prompt.render(view(cue(1, "Die Göttinn schwieg.")), unknown=["Göttinn"])
    assert "Die Göttinn schwieg." in text and "⚠" not in text


# ── the series ledger ────────────────────────────────────────────────────────

def test_the_ledger_promotes_the_most_written_spelling_whatever_the_reply_says():
    counts = {"Esdeath": 28, "Esteth": 13, "Estef": 10}
    assert spell_prompt.parse_ledger("Esteth = Esdeath, Estef", counts) == {
        "Esdeath": ["Estef", "Esteth"]}


def test_the_ledger_refuses_a_spelling_that_was_never_in_the_series():
    counts = {"Esdeath": 28, "Esteth": 13}
    assert spell_prompt.parse_ledger("Esdeath = Esteth, Esdeeth", counts) == {
        "Esdeath": ["Esteth"]}


def test_a_word_belongs_to_one_group_only():
    counts = {"Sihon": 18, "Siheon": 13, "Sihun": 4}
    names = spell_prompt.parse_ledger("Sihon = Siheon, Sihun\nSihun = Siheon", counts)
    assert names == {"Sihon": ["Siheon", "Sihun"]}


def test_a_group_of_one_is_not_a_group():
    assert spell_prompt.parse_ledger("Esdeath = Esdeath", {"Esdeath": 28}) == {}


def test_a_recurring_name_needs_no_dictionary_word_to_survive(monkeypatch):
    """Ordinary German words are dropped before anything is grouped: without that filter the
    loudest clusters in the corpus are grammar — Wenn/Denn, Wie/Wieso, Meine/Eine/Deine."""
    monkeypatch.setattr(spell, "unknown_words",
                        lambda words, cache=None: [w for w in words if w == "Esdeath"])
    cues = [cue(i, "Da kam Esdeath. Wenn Wenn.") for i in range(1, 5)]
    assert spell.candidates({"a.srt": cues}) == {"Esdeath": 4}


# ── applying it ──────────────────────────────────────────────────────────────

def test_every_timecode_survives_a_correction():
    original = [cue(1, "Die Göttinn schwieg.", "00:01:02,500"), cue(2, "Er ging.")]
    fixed = spell.apply(original, {1: "Die Göttin schwieg."})
    assert [c.timecode for c in fixed] == [c.timecode for c in original]


def test_no_other_cue_moves():
    original = [cue(1, "Die Göttinn schwieg."), cue(2, "Er ging."), cue(3, "Sie blieb.")]
    fixed = spell.apply(original, {2: "Er ging fort."})
    assert fixed[0] == original[0] and fixed[2] == original[2]


def test_an_index_that_was_never_in_the_file_cannot_enter_it():
    original = [cue(1, "Er ging.")]
    assert spell.apply(original, {99: "Etwas anderes."}) == original


# ── the record ───────────────────────────────────────────────────────────────

@pytest.fixture
def show(tmp_path, monkeypatch):
    """One episode of one show, with the cache pointed somewhere disposable."""
    monkeypatch.setattr(spell, "WORK_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(spell, "unknown_words", lambda words, cache=None: [])
    directory = tmp_path / "library" / "Dark"
    directory.mkdir(parents=True)
    primary = directory / "Dark - S01E01 - Primary.srt"
    srt.write(primary, [cue(1, "Die Göttinn schwieg."), cue(2, "die kinder schliefen.")])
    return primary


def _answers(reply):
    return lambda *a, **k: (reply, {}, 0.0)


def test_a_pass_corrects_the_file_and_journals_what_it_replaced(show, monkeypatch):
    from lib import claude
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    stats = spell.episode(show)
    assert stats["changed"] == 1
    assert srt.read(show)[0].text == "Die Göttin schwieg."
    record = spell.journal(show)
    assert record["edits"] == [{"cue": 1, "before": "Die Göttinn schwieg.",
                               "after": "Die Göttin schwieg."}]


def test_a_second_pass_over_a_corrected_episode_asks_nothing(show, monkeypatch):
    from lib import claude
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    monkeypatch.setattr(claude, "ask", _answers("1: Etwas ganz anderes hier."))
    assert spell.episode(show)["why"] == "already proofread"


def test_a_rubric_change_re_reads_the_episode(show, monkeypatch):
    from lib import claude
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    monkeypatch.setattr(spell_prompt, "FINGERPRINT", "changed!")
    monkeypatch.setattr(claude, "ask", _answers("2: Die Kinder schliefen."))
    assert spell.episode(show)["changed"] == 1


def test_a_failed_call_leaves_the_german_byte_for_byte(show, monkeypatch):
    from lib import claude
    from lib.config import UserError
    before = show.read_bytes()

    def refuse(*_a, **_k):
        raise UserError("the `claude` CLI isn't on PATH")

    monkeypatch.setattr(claude, "ask", refuse)
    stats = spell.episode(show)
    assert stats["changed"] == 0 and "PATH" in stats["why"]
    assert show.read_bytes() == before


def test_the_original_is_copied_before_the_first_request(show, monkeypatch):
    """Everything between the copy and the write can fail, and none of it may leave the
    ripped German unreachable."""
    from lib import claude
    before = show.read_bytes()
    seen = {}

    def ask(*_a, **_k):
        seen["copied"] = spell.orig_path(show).exists()
        raise RuntimeError("boom")

    monkeypatch.setattr(claude, "ask", ask)
    spell.episode(show)
    assert seen["copied"] and spell.orig_path(show).read_bytes() == before


def test_the_original_is_never_overwritten_by_a_second_pass(show, monkeypatch):
    from lib import claude
    ripped = show.read_bytes()
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    monkeypatch.setattr(spell_prompt, "FINGERPRINT", "changed!")
    monkeypatch.setattr(claude, "ask", _answers("2: Die Kinder schliefen."))
    spell.episode(show)
    assert spell.orig_path(show).read_bytes() == ripped


def test_revert_restores_the_ripped_german_byte_for_byte(show, monkeypatch):
    from lib import claude
    ripped = show.read_bytes()
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    assert show.read_bytes() != ripped
    assert spell.revert(show)
    assert show.read_bytes() == ripped


def test_revert_refuses_an_episode_that_moved_since_it_was_proofread(show, monkeypatch):
    from lib import claude
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    srt.write(show, [cue(1, "Etwas ganz anderes."), cue(2, "Und noch etwas.")])  # a hand edit
    assert not spell.revert(show)


def test_a_proofread_episode_is_not_pending_again(show, monkeypatch):
    from lib import claude
    root = show.parent.parent
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    assert spell.pending(root) == []
    assert spell.pending(root, redo=True) == [show]


def test_redo_re_reads_the_german_as_it_was_ripped(show, monkeypatch):
    """Otherwise corrections compound, and two rubrics can never be compared because each
    reads a different file — and comparing rubrics is exactly what --redo is for."""
    from lib import claude
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    seen = {}

    def ask(text, *_a, **_k):
        seen["asked"] = text
        return "1: OK\n2: OK", {}, 0.0

    monkeypatch.setattr(claude, "ask", ask)
    spell.episode(show, redo=True)
    assert "Die Göttinn schwieg." in seen["asked"]      # the ripped spelling, not the fixed one


def test_redo_keeps_a_hand_edit_instead_of_discarding_it(show, monkeypatch):
    from lib import claude
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    srt.write(show, [cue(1, "Von Hand geändert."), cue(2, "die kinder schliefen.")])
    seen = {}

    def ask(text, *_a, **_k):
        seen["asked"] = text
        return "1: OK\n2: OK", {}, 0.0

    monkeypatch.setattr(claude, "ask", ask)
    spell.episode(show, redo=True)
    assert "Von Hand geändert." in seen["asked"]


def test_a_word_may_be_replaced_by_a_settled_name_however_far_it_is():
    """The corpus's biggest defect class puts the mishearing nowhere near the name — measured
    on E19, `Estep → Esdeath` is 6 edits and `Susanne → Susanoo` is 4, both plainly right. The
    ledger is what makes this safe: the change can only produce a name the series already says."""
    assert spell_prompt.refuse("Generalin Estep kam.", "Generalin Esdeath kam.",
                               {"Esdeath"}) is None
    assert spell_prompt.refuse("Generalin Estep kam.", "Generalin Esdeath kam.")


def test_the_exemption_cannot_be_used_to_leave_a_name():
    """A settled name can be arrived at, never departed from."""
    assert spell_prompt.refuse("Generalin Esdeath kam.", "Generalin Estep kam.", {"Esdeath"})


def test_a_near_tie_is_left_to_the_reply():
    """Measured on A Time Called You: the ripped text has `Sihon` 11 against `Siheon` 12.
    Overriding a model that can read Korean romanisation on a one-vote margin is overriding
    it with a coin toss, so below MAJORITY the reply's own choice stands."""
    counts = {"Siheon": 12, "Sihon": 11, "Sihun": 4}
    assert spell_prompt.parse_ledger("Sihon = Siheon, Sihun", counts) == {
        "Sihon": ["Siheon", "Sihun"]}
    assert spell_prompt.parse_ledger("Siheon = Sihon, Sihun", counts) == {
        "Siheon": ["Sihon", "Sihun"]}


def test_a_clearly_rarer_spelling_is_still_never_made_canonical():
    assert spell_prompt.parse_ledger("Esteth = Esdeath", {"Esdeath": 28, "Esteth": 6}) == {
        "Esdeath": ["Esteth"]}


def test_the_ledger_reads_the_ripped_german_not_the_corrected_files(show, monkeypatch):
    """The defect this pins made the ledger confirm its own edits: built from the corrected
    files, the spelling it chose last episode is the spelling it sees winning this episode.
    Measured, that turned a 11-vs-12 coin toss into a manufactured 4-vs-33."""
    from lib import claude
    monkeypatch.setattr(claude, "ask", _answers("1: Die Göttin schwieg.\n2: OK"))
    spell.episode(show)
    assert srt.read(show)[0].text == "Die Göttin schwieg."          # the file has moved on
    ripped = spell._show_cues(show)[show.name]
    assert ripped[0].text == "Die Göttinn schwieg."                 # the ledger has not


def test_a_variant_needs_less_evidence_than_a_canonical(monkeypatch):
    """Measured on Crash Landing on You: `Samsuk` 4, misheard as `Samsung` 2, `Samso` 2,
    `Sambuk` 2. Under one shared floor no mishearing was a candidate, no cluster formed, and
    the pass fixed 1 of the 4 occurrences. The canonical must clear the bar; the mistake need
    not."""
    # Only the name-shaped tokens are unknown to the dictionary — `Da` is ordinary German,
    # and a stub that flags everything makes the filler a candidate and the test a lie.
    monkeypatch.setattr(spell, "unknown_words",
                        lambda words, cache=None: [w for w in words if w.startswith("Sams")])
    text = " ".join(["Da Samsuk"] * 4 + ["Da Samsung"] * 2 + ["Da Samso"] * 2)
    cues = [cue(1, text)]
    got = spell.candidates({"a.srt": cues})
    assert got == {"Samsuk": 4, "Samsung": 2, "Samso": 2}


def test_a_name_that_is_also_a_german_word_still_reaches_the_ledger(monkeypatch):
    """Measured on Move to Heaven: the protagonist is written `Guru` 26 times and `Guro` 10,
    and `Guru` is also the German word. The dictionary filter hid it, the ledger saw only
    `Guro`, and the pass moved three occurrences from the majority spelling to the minority."""
    monkeypatch.setattr(spell, "unknown_words",
                        lambda words, cache=None: [w for w in words if w != "Guru"])
    cues = [cue(1, "Der Guru " * 6 + "Der Guro " * 2)]
    got = spell.candidates({"a.srt": cues})
    assert got["Guru"] == 6 and got["Guro"] == 2


def test_an_ordinary_german_word_takes_a_name_only_with_a_clear_margin():
    """The other side of that pull-in: `Guru` may be the name, but `Serie` must not be able to
    swallow `Seri` just by being commoner in German."""
    assert spell_prompt.parse_ledger("Guro = Guru", {"Guru": 26, "Guro": 10}, {"Guru"}) == {
        "Guru": ["Guro"]}
    assert spell_prompt.parse_ledger("Serie = Seri", {"Serie": 14, "Seri": 9}, {"Serie"}) == {
        "Seri": ["Serie"]}


def test_a_group_of_only_german_words_settles_nothing():
    assert spell_prompt.parse_ledger("Serie = Serien", {"Serie": 14, "Serien": 9},
                                     {"Serie", "Serien"}) == {}


def test_a_name_may_not_move_to_its_rarer_spelling():
    """The first wrong edit the sweep produced, and the class behind it. Rising Impact writes
    `Kiria` 22 times, its genitive `Kirias` twice, and `Kiriya` **once** — and the pass rewrote
    a `Kirias` into `Kiriyas`, a spelling the series never uses at all, adding a fourth. Move to
    Heaven's `Guru`(26) → `Guro`(10) is the same vector. The ledger caught neither, because it
    had settled neither; counting is something the caller can always do."""
    counts = {"Kiria": 22, "Kirias": 2, "Kiriya": 1, "Guru": 26, "Guro": 10}
    assert spell_prompt.refuse("Frau Kirias Version", "Frau Kiriyas Version", (), counts)
    assert spell_prompt.refuse("Papa. Und Guru.", "Papa. Und Guro.", (), counts)


def test_the_count_guard_leaves_an_ordinary_spelling_fix_alone():
    """Floored at VARIANT_MIN on the original, so a word written once — which is most of
    them — is never refused for want of a second occurrence."""
    counts = {"Füsse": 1, "Esteth": 9, "Esdeath": 12, "Kaiserwassen": 4, "Kaiserwaffen": 40}
    assert spell_prompt.refuse("Die Füsse!", "Die Füße!", (), counts) is None
    assert spell_prompt.refuse("Da Esteth kam", "Da Esdeath kam", (), counts) is None
    assert spell_prompt.refuse("Von Kaiserwassen", "Von Kaiserwaffen", (), counts) is None


def test_the_ledger_overrides_the_count_guard():
    """If the series has settled on a spelling, that is the direction to move in even when it
    is the rarer one — the ledger is the better evidence and it had the model's reading."""
    counts = {"Guru": 26, "Guro": 10}
    assert spell_prompt.refuse("Und Guru.", "Und Guro.", {"Guro"}, counts) is None


def test_word_counts_counts_a_name_at_the_start_of_a_sentence():
    """Unlike `_mentions`, which drops those on purpose — a character is addressed at the start
    of a sentence more than anywhere else, and this is asked how a name is usually spelled."""
    assert spell.word_counts({"a.srt": [cue(1, "Guru. Und Guru sagte nichts.")]})["Guru"] == 2
