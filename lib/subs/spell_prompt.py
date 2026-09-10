"""The proofreading rubric, the reply contract, and every guard that stands under it.

Pure stdlib and pure logic, like `naming` and `episode_range`: no AppKit, no `claude`, no
files. That is what lets `tests/test_subs_spell.py` reach the part where the safety lives
without a browser, a dictionary or a subscription.

**What this pass is for.** `gigaku subs` rips its Primary from Language Reactor's *ASR Pro
German* — a machine transcript of the German dub, with machine mistakes in it. Measured over
the 165 ripped episodes (72,525 cues, 2.88 MB) before any of this was written, Language
Reactor's own export is byte-faithful: 0 mojibake, 0 stripped umlauts (34,071 `ä/ö/ü/ß` all
present), 0 run-together words, 0 `ss` for `ß`, 0 markup. Nothing is wrong with the export.
What is wrong is the transcript, in four measured classes:

* **A name spelled differently every time it is said** — the commonest defect and the one a
  reader notices first, because it is the word they would go and look up. In one show:
  `Esdeath` 28×, `Esteth` 13×, `Estef` 10×, plus `Estet`, `Esdeth`, `Esteph`, `Estep`. Also
  `Sihon`/`Siheon`/`Si-heon`/`Sihun`/`Si-Hon`, `Tatsumi`/`Ttsumi`/`Tatsomi`/`Tatsumee`,
  `Kaiserwaffe`/`Kaiserwassen`/`Kaiserwachen`/`Kaiserwappe`. ~28 minority occurrences an
  episode. This is why a request carries the show's name ledger (`render`'s ``known``): the
  model cannot see the other 400 cues, and the majority spelling is a fact only the caller has.
* **Ordinary misspellings**, ~3–5 an episode: `unernwartet`, `zerstöckelt`, `Medikement`,
  `Prinzessine`, `desqualifiziert`, `sturköpfig`, `lebendisch`, `Göttinn`.
* **A German noun the transcriber left lowercase**, ~2.4 an episode: `ankunft`, `kinder`,
  `königreichen`, `arbeit`, `kaffee`. Easy to read past when writing a rubric, and the
  easiest of the four to get right, so it is named explicitly below.
* **Homophones**, rare — 69 `, das ich…` for `dass` across the whole corpus.

**Why the whole line is read rather than a list of suspect words.** A cheaper design was
measured and rejected: macOS's German dictionary nominates ~63 tokens an episode and one call
decides them. It is ~8× cheaper and it cannot see a homophone, a missing capital on a word
the dictionary knows in lowercase, or a name that is *consistently* wrong. Reading every line
is the user's decision of 2026-09-04, taken with the price in front of them.

**Why this is not a rule in the glosser's rubric.** `lib/subs/CLAUDE.md` records the lesson
twice over, from `_unjoin` and `_normalise`: a rule the glosser honours for some occurrences
and not others is not fixed by stating it an eighth time — it is glossing 350 lines and
spending its attention on meaning, which is where it belongs. `translate_prompt` goes further
and *forbids* repair ("Translate what is written, not what the plot needs… Do not invent a
line that makes better sense than the transcript"), so fixing the German upstream is what
lets that rule stand as written instead of arguing with it.
"""
import difflib
import hashlib
import re

# `→` asks for an answer, `·` is context — the same two glyphs `translate_prompt` uses, for
# the same reason: a glyph cannot be read as part of the German line, a word could.
ASK, CONTEXT = "→", "·"

# The verdict for a line that needs nothing. A word rather than a glyph because it is the
# answer the model gives hundreds of times an episode and it must be unmistakable.
KEEP = "OK"


SYSTEM = """\
You are proofreading German subtitles.

The text is a machine transcript (ASR) of the German dub of an anime or a Korean drama. It \
was produced by speech recognition, so it has speech-recognition mistakes in it: misspelled \
words, nouns left lowercase, proper names spelled a different way each time, and the odd \
homophone. Punctuation is the transcriber's guess. One line is one subtitle cue, and a \
sentence is routinely cut across two or three cues.

Your job is to correct the spelling of what is written. Nothing else.

# The answer

For each line marked → you answer one line, starting with that line's id:

    128: OK
    129: Die Göttin gab ihm ihren Segen.

`OK` means the line needs no change. Otherwise give the whole corrected line, once, exactly \
as it should stand in the file.

Answer only these lines. No preamble, no commentary, no blank lines, no markdown, no quotes \
around the line. Lines marked · are there so you can see the sentence a cue was cut out of; \
they have no id and you never answer them.

# The default is OK, because the two mistakes do not cost the same

If you are not sure, answer OK.

A typo you leave is one odd word the reader reads past. A word you "correct" wrongly is \
worse in every way: it is a word that was right, it is now wrong, and if it is a name it is \
wrong in all forty of its appearances — and the reader, who is learning German from these \
subtitles, will go and look it up and find nothing.

Most lines are already correct. Answering OK to a line that needed nothing costs nothing.

# Correct

**A misspelled word.** `Göttinn` → `Göttin`. `Medikement` → `Medikament`. `Prinzessine` → \
`Prinzessin`. `desqualifiziert` → `disqualifiziert`. `zerstöckelt` → `zerstückelt`. \
`unernwartet` → `unerwartet`. `sturköpfig` → `starrköpfig`.

**A German noun left lowercase.** German capitalises its nouns and the transcriber often \
does not: `ankunft` → `Ankunft`, `kinder` → `Kinder`, `königreichen` → `Königreichen`, \
`arbeit` → `Arbeit`, `kaffee` → `Kaffee`. Correct it wherever it stands in the line. Do not \
capitalise anything else: a line that begins in the middle of a sentence begins with a small \
letter and that is correct.

**A name spelled against the ledger.** If the request opens with a list of names, those \
spellings are settled for this whole series. A line saying `Esteth` where the ledger says \
`Esdeath` gets `Esdeath`. This is the single commonest defect in these files.

**A homophone that is plainly the wrong word.** `, das ich` → `, dass ich`. `seid` for \
`seit`, `wieder` for `wider`. Only where the grammar makes it certain.

**A word split or joined wrongly.** `Ihmeine` → `Ihm eine`, `gegens Trinken` → `gegen das \
Trinken`.

# Leave alone

**Proper names, place names and invented words** — these are anime and Korean drama and \
most of the cast is named in neither German nor English. `Midgar`, `Incursio`, `Susanoo`, \
`Namra`, `Gwinam`, `Kaiserwaffe`, `Shadowgarden`, `Blue Lock`. If a name is not in the \
ledger and nothing in the request says what it should be, it stands as written.

**Dialogue in another language.** These scripts drop into English and Korean and Japanese \
on purpose: `Let's go`, `Nice to meet you`, `Arigato`, `Nee-san`, `-sama`, `-chan`. Not \
German, not a mistake, not yours.

**Spoken and dialect German the dictionary does not carry.** `bitteschön`, `dankeschön`, \
`hätteste`, `rumhockt`, `hochleveln`, `gescoutet`, `rauskicken`, `Normalo`, `aggro`, `Ähm`, \
`Häh`. People speak like this. Do not tidy it into written German.

**Speech that is broken on purpose** — a stammer, an interruption, a character who talks \
badly. Repairing it deletes the characterisation.

**Everything about the line that is not spelling.** Word order, grammar, register, style, \
punctuation you merely disagree with, a line you would have phrased better.

# Correct a spelling, never a hearing

Where the transcriber heard the wrong word entirely — a name mangled into nonsense, a word \
that fits nothing in the scene — that is not yours to fix. You cannot hear the audio and \
guessing what was said is how a subtitle file acquires a sentence nobody spoke. If a line \
needs more than its spelling changed to be right, answer OK and leave it.

The test for every change you make: **could the same words, spelled correctly, be what is \
already written here?** If the answer needs a different word, it is a hearing, not a \
spelling, and you answer OK.

# The shape of a correction

Your line replaces the one line it answers, in that one cue. You are not rewriting it, not \
translating it, not shortening it, not adding a word, not deleting one, and not changing the \
grammar around the word you fixed. Everything you do not correct comes back exactly as it \
was given to you, character for character.
"""

# Short fingerprint of the rubric, stored in the journal so an episode can say which wording
# produced its corrections — and so a re-run after an edit is a decision rather than an
# accident. Same construction as `translate_prompt.FINGERPRINT`.
FINGERPRINT = hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]


LEDGER = """\
You are given the recurring capitalised words of one series' German subtitles, with how often \
each is written that way. The subtitles are a machine transcript (ASR), so one name is often \
written several different ways across a series.

Group the spellings that are the same name, and say which spelling is right.

Answer one line per group, and only for groups that have more than one spelling:

    Esdeath = Esteth, Estef, Estet, Esdeth
    Najenda = Nagenda, Narzenda

Left of the `=` is the correct spelling. Right of it are the misspellings of it, comma \
separated. Use only words from the list, exactly as they are written there.

Say nothing about a word that has only one spelling. No preamble, no commentary, no \
markdown.

# What is one name and what is two

The counts are your best evidence: a spelling written forty times is how the name is really \
written, and a spelling written three times that looks like it is a mishearing of it.

Two different characters can have similar names, and merging them is far worse than leaving \
them apart — it renames somebody. `Kuro` and `Kurome` are two people. `Koro` and `Kuro` are \
two people. `Bulat` and `Bulik` are two people. If you are not confident that two spellings \
are the same name, leave them out.

A name with an honorific or a suffix attached is **not** a misspelling of the bare name: \
`Esdeath-sama`, `Akame-chan`, `Nee-san`, `Susanoo-Menü` all stand on their own.

A grammatical form is not a misspelling either: `Tatsumis` is `Tatsumi` in the genitive and \
`Kaiserwaffen` is the plural of `Kaiserwaffe`. Leave both alone.

Ordinary German words that happen to be in the list — a noun, a title, a place that really \
exists — are not names to be grouped. Leave them out.
"""

# Fingerprinted apart from the proofreading rubric: they are asked different questions, cached
# in different places, and an edit to one must not re-do the other's work.
LEDGER_FINGERPRINT = hashlib.sha1(LEDGER.encode()).hexdigest()[:8]

# `canonical = variant, variant`. The gaps are `[ \t]` for the reason `_VERDICT` records.
_GROUP = re.compile(r"^[ \t]*(?:[→\-*][ \t]*)?([^\s=]+)[ \t]*=[ \t]*(\S[^\n]*?)[ \t]*$", re.M)


def render_ledger(counts):
    """The user turn for the once-per-series ledger call. ``counts`` is {word: times said}."""
    listed = sorted(counts, key=lambda w: (-counts[w], w))
    return (
        f"{len(listed)} words from this series' subtitles, most-written first.\n\n"
        + "\n".join(f"{word} ×{counts[word]}" for word in listed)
    )


def parse_ledger(text, counts, weak=()):
    """``{canonical: [variant, …]}`` — the groups that survive their guards.

    Four of them, each answering a way the reply can be *wrong* rather than absent. They are
    the same shape as `parse_normalise`'s, and they matter more here than anywhere else in
    this module, because the ledger is handed to `refuse` as names that may not be respelled:
    a bad group does not merely miss a fix, it **protects a mistake**.

    1. **Only words that were listed**, on both sides. An invented spelling can never enter
       the ledger, and so can never enter the file.
    2. **A clearly rarer spelling is never made canonical**, whatever the reply says — but a
       near-tie is left to the reply. The grouping is what is hard and what the model is being
       asked for; frequency is arithmetic the caller already has, and letting a reply promote
       a *rare* spelling over a common one is the one way this could make a series worse in
       every episode at once. But frequency is only evidence when there is some: measured on
       A Time Called You, the ripped text has `Sihon` 11 against `Siheon` 12, and overriding a
       reply on a one-vote margin is overriding a model that can read Korean romanisation with
       a coin toss. So the override fires only when the most-written spelling is at least
       `MAJORITY` times as common as the one the reply chose; below that the reply stands.
    3. **A word belongs to one group.** The first group claiming it wins, so a reply that
       lists a name twice cannot leave two canonical spellings of it standing.
    4. **A group of one is not a group.** A canonical with no variants left after the guards
       says nothing and is dropped.
    """
    known = set(counts)
    out, taken = {}, set()
    for match in _GROUP.finditer(text or ""):
        head = match.group(1).strip().strip(",")
        variants = [v.strip() for v in match.group(2).split(",")]
        members = [w for w in [head] + variants if w in known and w not in taken]
        if len(members) < 2 or head not in members:
            continue
        taken.update(members)
        top = max(members, key=lambda w: (counts[w], -len(w)))
        canonical = top if counts[top] >= MAJORITY * counts[head] else head
        # ``weak`` holds spellings that are ordinary German words. One of those may well be the
        # right name — `Guru` is both a German noun and how a series writes 한그루 — but it is
        # also how a real word gets mistaken for a name, so it becomes canonical only with a
        # clear margin over every rival. Below that the best non-German spelling wins, and a
        # group with no such spelling settles nothing.
        if canonical in weak and any(counts[m] * MAJORITY > counts[canonical]
                                     for m in members if m != canonical):
            plain = [m for m in members if m not in weak]
            if not plain:
                continue
            canonical = max(plain, key=lambda w: (counts[w], -len(w)))
        rest = [w for w in members if w != canonical]
        if rest:
            out[canonical] = sorted(rest)
    return out


def render(view, names=(), unknown=()):
    """The user turn for one request. ``view`` is [(cue, needed), …] in file order.

    ``names`` is the series' name ledger, already written out one line per name, and
    ``unknown`` the tokens macOS's German dictionary
    does not recognise in these lines. Both are facts the model structurally cannot compute
    from what it is shown: the ledger is spread over the other four hundred cues of the show,
    and the dictionary is not in its head.

    **The dictionary's tokens are listed, never marked inside the text.** A `⚠` next to a
    word inside the German is a character the model can echo back into the file, and this
    pass's whole promise is that the line comes back as it was given but for the spelling.
    It is a list of things to look at, and it is said in as many words that it is not a list
    of things to change — the dictionary is wrong about names constantly (measured: it
    suggests `Barou`→`Balou`, `Susanoo`→`Susana`, `Reo`→`Leo`, `Midgar`→`Midgard`, reaching
    for the nearest word it knows), which is exactly why its *suggestions* are not passed on
    at all and only its doubts are.

    Context lines carry no id, so a model that cannot see an id cannot echo one — the same
    free guard `translate_prompt.render` relies on.
    """
    wanted = [cue for cue, needed in view if needed]
    lines = [
        f"{ASK} {cue.index}: {cue.text}" if needed else f"{CONTEXT} {cue.text}"
        for cue, needed in view
    ]
    # Ahead of the lines, not after them: both are constraints on the answer and the request
    # is read top to bottom. Stated as instructions rather than left as bare lists, because a
    # list of words with no verb reads as vocabulary rather than as a rule.
    header = ""
    if names:
        header += (
            "Names in this series, already settled. Spell each of them exactly like this, and "
            "where a line writes one of the misspellings after `never`, correct it:\n"
            + "\n".join(names) + "\n\n"
        )
    if unknown:
        header += (
            "Words in the lines below that the German dictionary does not recognise. Most "
            "are names or spoken German and are correct as they stand; this is a list of "
            "words to look at, not a list of words to change:\n"
            + ", ".join(unknown) + "\n\n"
        )
    count = len(wanted)
    return (
        header
        + f"Proofread the {count} line{'' if count == 1 else 's'} marked {ASK}.\n"
        f"Answer with {count} line{'' if count == 1 else 's'}, one per id: "
        f"`id: {KEEP}`, or `id: ` and the corrected line.\n\n"
        + "\n".join(lines)
    )


# The id, then the verdict. Anchored at the line start so prose can't be mined for pairs.
#
# **Every gap is `[ \t]`, never `\s`, and that is load-bearing** — the lesson
# `translate_prompt._LINE` paid for: `\s` matches a newline, so a bare `1:` followed by
# `2: Ja.` parses as *id 1 with the text "2: Ja."*, an unanswered line silently eating the
# next line's answer. That is the one failure the echoed id exists to make impossible.
_VERDICT = re.compile(r"^[ \t]*(?:[→\-*][ \t]*)?(\d{1,6})[ \t]*[:.][ \t]*(\S[^\n]*?)[ \t]*$",
                      re.M)

# A model that wraps its answer in quotes is not proposing quotation marks; it is quoting.
# Stripped rather than refused, and only when the original had none, so a line that really
# does open with a quote keeps it.
_QUOTED = re.compile(r'^(["“„«\'])(.*)(["”“»\'])$')

# Per changed run of words. Every real correction measured on this corpus is 1–2 edits
# (`Göttinn→Göttin` 1, `Medikement→Medikament` 1, `zerstöckelt→zerstückelt` 1,
# `desqualifiziert→disqualifiziert` 1, `gegens→gegen das` 2); `Incursio→Inquisition` is 6.
MAX_WORD_EDITS = 3

# How much more often the most-written spelling of a name must be said before the caller
# overrules the reply's own choice of canonical. Two, because that is the smallest margin that
# is plainly a majority rather than noise: on the corpus, real ties sit at 11-vs-12.
MAJORITY = 2
# The floor `refuse` uses before it will call one spelling better attested than another. Kept
# equal to `spell.VARIANT_MIN` on purpose: below it a spelling is not evidence of anything.
VARIANT_MIN = 2

# Punctuation that hangs off a word without being part of it.
_EDGES = "\"'“”„«»‚‘’()[]{}.,!?…:;-–—"


def skeleton(text):
    """Only the letters, lower-cased — what a spelling fix is allowed to leave standing.

    Umlauts are deliberately **not** folded. `ä`→`a` is a real error class in German and
    folding it would hide exactly the mistakes this pass exists to catch, so `ä` and `a` are
    two different letters here as they are in the language.

    Case and punctuation drop out on purpose: `ankunft`→`Ankunft` is the commonest true
    correction in these files, and a guard that measured it as a change would have to be
    loosened until it stopped guarding anything.
    """
    return "".join(ch.lower() for ch in text if ch.isalpha())


def _edits(a, b):
    """Levenshtein distance. Small strings only — this is called per changed word run."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _words(text):
    """The line as bare words — punctuation stripped, so a name is the same token whether or
    not a comma happens to follow it. `'Midgar' in 'Der Weg nach Midgar.'.split()` is False,
    which is the sort of thing that makes a guard quietly stop guarding."""
    return [w.strip(_EDGES) for w in text.split()]


def refuse(original, corrected, protected=(), counts=None):
    """``None`` if ``corrected`` may replace ``original``; otherwise why it plainly may not.

    Returns a whole reason, ready for the journal — the shape `lib/subs/language.py::mismatch`
    already uses, and for the same reason: a refusal that can only say "no" teaches nobody
    anything, and this one is read while the rubric is being tuned.

    **These are backstops, not the judgement.** The rubric decides whether a word is a typo or
    a name; these guards only make the answers the rubric cannot give structurally impossible
    to write into the file. If you find yourself tightening them instead of the prose, the
    rubric is what is wrong.

    The two budgets are deliberately split — **how much of the line moves**, and **how far
    each piece of it moves** — and the first version of this got that wrong in a way worth
    recording, because it looked right and was measured wrong within the minute. It budgeted
    the whole line's letters at ``len // 8``, which on `Esteth kam allein.` is one edit, and
    `Esteth → Esdeath` is two. That is not an edge case: respelling a name is the single
    commonest correction in these files, and a line-length budget refuses it on short lines
    and waves it through on long ones — precisely backwards, since a short line is where a
    two-letter fix is *most* obviously a spelling.

    1. **One line, not empty.** A reply with a newline in it is answering a different question.
    2. **How much moves**: at most ``max(2, words // 6)`` runs of the line may change. A
       paraphrase, a translation or "the line the plot needed" moves nearly every run and
       cannot be expressed.
    3. **How far each moves**: every changed run is within `MAX_WORD_EDITS` letters of its
       counterpart, compared case-insensitively so a capitalisation fix costs nothing. A word
       swapped for an unrelated one, or a phrase appended out of nowhere, is refused here even
       when guard 2 had room for it. Splits and joins pass, because the letters on both sides
       are nearly the same.
    4. **A settled name is untouchable.** ``protected`` is the show's name ledger; a name it
       holds must come back spelled the way it went in. The one guard aimed at a specific
       defect rather than at a shape, and it earns that because corrupting a name is the worst
       thing this pass can do: measured, macOS's own German dictionary wants `Barou`→`Balou`,
       `Susanoo`→`Susana`, `Reo`→`Leo` and `Midgar`→`Midgard`, and a model reaching for the
       nearest German word makes the same mistake in the same places. It is one-directional by
       construction — it protects a name that was **already in the line**, so correcting a line
       *towards* a ledger name, which is what the ledger is for, is untouched by it.
    """
    if "\n" in corrected or "\r" in corrected:
        return "the answer is more than one line"
    if not corrected.strip():
        return "the answer is empty"

    old, new = original.split(), corrected.split()
    runs = max(2, len(old) // 6)
    changed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new,
                                                       autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        changed += 1
        if changed > runs:
            return f"{changed} parts of the line changed, {runs} allowed — that is a rewrite"
        was, now = " ".join(old[i1:i2]), " ".join(new[j1:j2])
        # **A word replaced by a settled name is exempt from the distance guard**, and only
        # from that one. The ledger holds spellings this series demonstrably uses, so the
        # change can only ever produce a name the show already says — while what it fixes is
        # the corpus's largest defect class, where the mishearing is nowhere near the name:
        # measured on E19, `Estep → Esdeath` is 6 edits and `Susanne → Susanoo` is 4, both
        # plainly right and both refused by a flat distance. The exemption is one-directional
        # by construction (a name can be arrived at, never departed from — guard 4 below sees
        # to that), and guard 2 still caps how much of the line may move at all.
        if set(_words(now)) <= set(protected) and _words(now):
            continue
        moved = _edits(skeleton(was), skeleton(now))
        if moved > MAX_WORD_EDITS:
            shown = f"{was!r} → {now!r}" if was and now else repr(was or now)
            return f"{shown} is a different word, not a spelling of the same one"

    kept = set(_words(corrected))
    for name in protected:
        if name in _words(original) and name not in kept:
            return f"{name!r} is a settled name in this series and may not be respelled"

    # **A name may never move from a well-attested spelling to a rarer one.** The ledger stops
    # this only for the names it settled, and the two cases that got through were both ones it
    # never saw: `Guru`(26) → `Guro`(10), where the name collides with a German word, and
    # `Kirias`(2) → `Kiriyas`(**0**), where the spelling being copied appears once in the whole
    # series and the result is a fourth spelling of one name. Counting is something the caller
    # can always do, so this holds whether or not the ledger noticed — the ledger's job is to
    # say what a name *should* be, this one's is to refuse making a series less consistent than
    # it was found.
    #
    # Scoped to name-shaped words (capitalised, and unknown to German on one side or the other)
    # so an ordinary spelling fix is untouched, and floored at `VARIANT_MIN` on the original so
    # a one-off like `Füsse` → `Füße` is never refused for want of a second occurrence.
    if counts:
        old_w, new_w = _words(original), _words(corrected)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_w, new_w,
                                                           autojunk=False).get_opcodes():
            if tag != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
                continue
            was, now = old_w[i1], new_w[j1]
            if not was[:1].isupper() or now in protected:
                continue
            before, after = counts.get(was, 0), counts.get(now, 0)
            if before >= VARIANT_MIN and after * MAJORITY < before:
                return (f"{was!r} is written {before}× in this series and {now!r} {after}× — "
                        f"a name does not move to its rarer spelling")
    return None


def parse(text, view, protected=(), counts=None):
    """``id: verdict`` lines → ``{id: corrected text}``, holding only accepted changes.

    ``view`` is the request's own [(cue, needed), …], so the parser can compare each answer
    against the line it is answering — which is what every guard in `refuse` needs.

    Three things this deliberately does *not* do, each the opposite of `translate_prompt.parse`
    and each the safe direction for this pass rather than for that one:

    * **A line that never comes back is a keep, not a gap.** For the glosser a missing line is
      a hole in the Secondary and stops the episode; here the cue simply stands as it was
      ripped, which is the state the file was already in. The caller re-asks once anyway
      (`SUBS_SPELL_RETRIES`), so a truncated reply is still noticed — it just isn't fatal.
    * **`OK`, and any answer equal to the line, produce no entry at all.** The result is the
      set of *changes*, so an episode where nothing was wrong writes nothing and hashes the
      same.
    * **A refused answer produces no entry either**, and the reason is handed back beside the
      changes so it can be journalled. A guard that silently dropped its refusals would make
      the rubric impossible to tune.

    Returns ``(changes, refusals)``.
    """
    lines = {cue.index: cue.text for cue, needed in view if needed}
    changes, refusals = {}, []
    seen = set()
    for match in _VERDICT.finditer(text or ""):
        index, verdict = int(match.group(1)), match.group(2).strip()
        if index not in lines or index in seen:
            continue          # never asked for, or already answered — first answer wins
        seen.add(index)
        original = lines[index]
        if verdict.upper() == KEEP or verdict == original:
            continue
        quoted = _QUOTED.match(verdict)
        if quoted and not _QUOTED.match(original):
            verdict = quoted.group(2).strip()
            if verdict == original:
                continue
        why = refuse(original, verdict, protected, counts)
        if why:
            refusals.append({"cue": index, "before": original, "after": verdict, "why": why})
            continue
        changes[index] = verdict
    return changes, refusals


def unanswered(text, view):
    """Ids that were asked for and never came back — what a re-ask is for."""
    asked = [cue.index for cue, needed in view if needed]
    answered = {int(m.group(1)) for m in _VERDICT.finditer(text or "")}
    return [index for index in asked if index not in answered]
