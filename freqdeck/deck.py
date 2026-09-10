#!/usr/bin/env python3
"""A study deck from the head of a frequency list — sentences by Opus, audio by TTS, one apkg.

    uv run --with genanki --with simplemma python freqdeck/deck.py all --n 1000
    uv run --with genanki --with simplemma python freqdeck/deck.py all --n 1000 --list yt
    uv run --with genanki --with simplemma python freqdeck/deck.py media      # Anki running

or stage by stage: sentences → check → translate → tts → wordaudio → apkg, then media.
Then anki_import.py (inside Anki, via oneshot_addon), definitions.py, R, audit.py — the
whole order and every measured reason is in freqdeck/README.md.

Every stage is cached in freqdeck/out/ and skips what already exists, so a re-run costs
nothing and a rate limit costs one request (ru_fill.py's shape). To extend the deck, raise
--n: only the new words are asked for, and the apkg's guids are the words themselves, so
re-importing UPDATES the existing notes (MvJ's importer, preserve_guids: a non-empty
source field replaces, an empty one leaves the live value) and adds the rest.

What each stage does and why it is shaped like that:

sentences  Three candidate sentences per word from Opus, GROUP words per request (50
           words = 150 lines ≈ 27k output tokens; the ~24k-token harness is what needs
           amortising — lib/claude.py — and 200-line answers are where a call starts to
           fail). The rubric (SYSTEM below) is the whole product: spoken German a person
           would say, built from words the learner already knows, and yet forcing the
           target's meaning from the situation alone. Three candidates because a single
           draw is not a measurement — the
           choice is made by `check`, not by whoever wrote first. Effort is pinned
           (`high`) for the bimodal-answer reason lib/claude.py records. Every request
           carries the WHOLE target list as "words he does not know", so a sentence
           for word 20 cannot lean on word 300.
check      Two independent judges over every candidate, then a pick. (1) Vocabulary,
           mechanically: simplemma lemmatises each token, and a token is flagged when
           de-words.tsv lists it AND no form of it is in the Migaku known set
           (known_words(), i.e. `gigaku words --lang de --stage known`; see
           `flags` for why either witness alone is noise). (2) Clarity, by the existing
           German i+1 rubric lib/anki/prompt_de.py, unchanged: it already answers
           exactly "how much of the target's meaning does this sentence hand the
           learner", rated within a word block so the three candidates are compared,
           not absolutely placed; ~150 items per call, a word never split
           (`prompt.groups`). The pick is fewest flags, then highest clarity, then
           shorter — written to chosen.tsv, which is hand-editable; the later stages
           read only that file. The full report goes to check-report.txt.
translate  A natural Russian translation of every chosen sentence (→ Notes, the field
           MvJ's own translation lands in). Opus, echoed ids, 250 per request; the
           target's sense in the sentence must survive the translation, that is the
           one thing the rubric insists on.
tts        OpenAI `tts-1`, voice `shimmer`, speed 1.0, mp3 — byte-for-byte the request
           the MvJ add-on makes for German definition audio (mvj/services/tts.py,
           `german_tts` in its meta.json), so the sentence and its definition are one
           voice. The key is read from the add-on's own meta.json, never copied here.
           Files are named freq_de_<nnn>_<ascii>.mp3, lowercase (umlauts folded: an
           NFD/NFC mismatch between macOS and Anki's media check is a classic
           missing-file; Anki lowercases stored names — see ascii_slug).
wordaudio  Native recordings from Wikimedia Commons (De-<Wort>.ogg, the add-on's own
           source — mvj/services/german_audio.py), but asked through the MediaWiki
           API 50 titles at a time instead of one HEAD per word: the 💣's per-word
           probes get 429 (Retry-After 600) after a dozen, and 37 of the first 50
           words fell to TTS although 21 of them have a recording. Pre-filled in the
           apkg, so the 💣 finds Word Audio already there and never TTS-es it; a word
           with no recording is left empty and the 💣's TTS fallback fills it.
apkg       genanki on the 16-field 🇩🇪 list (apkg_export_de.py's, unchanged — the
           FIELDS list must match the live notetype). Filled: Sentence, Word, Notes,
           Sentence Audio and Word Audio ([sound:] — MvJ's importer rewrites it to
           [audio:], which is what the template plays). guid = guid_for("freq-de:
           <word>"). No media bundled (the MvJ way: bundling once shipped 700 MB into
           collection.media).
media      Pushes the mp3s and oggs into collection.media over AnkiConnect — see
           stage_media for why it is that folder and not MvJ's database.media.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from lib.claude import ask, describe  # noqa: E402

OUT = os.path.join(HERE, "out")                       # everything generated (gitignored)
KNOWN = os.path.join(OUT, "de-known.txt")             # `gigaku words --lang de --stage known`, refreshed by known_words()
MEDIA = os.path.join(OUT, "media")

# One deck, several source lists. `words` is the general spoken-German frequency list;
# `yt` is the vocabulary of the channels the user actually watches, prepared by wordlist.py
# (see freqdeck/README.md for why the second thousand comes from there rather than from
# this one's tail). Each list keeps its own caches — a word is unique across them (the
# guid is the word), but a *rank* is not, and the media file name carries the rank.
LISTS = {
    "words": {"file": "freq/de-words.tsv", "prefix": "freq_de", "cache": ""},
    "yt": {"file": "freq/de-yt-deck.tsv", "prefix": "yt_de", "cache": "yt-"},
}
LIST = "words"
WORDS = PREFIX = CANDIDATES = SCORES = CHOSEN = TRANSLATIONS = WORDAUDIO = REPORT = None


def use_list(name):
    """Point the module at one source list. Called by main(); tests call it too."""
    global LIST, WORDS, PREFIX, CANDIDATES, SCORES, CHOSEN, TRANSLATIONS, WORDAUDIO, REPORT
    spec = LISTS[name]
    LIST, PREFIX = name, spec["prefix"]
    WORDS = os.path.join(ROOT, spec["file"])
    c = spec["cache"]
    CANDIDATES = os.path.join(OUT, f"{c}candidates.tsv")      # word ⇥ k ⇥ sentence
    SCORES = os.path.join(OUT, f"{c}scores.tsv")              # word ⇥ k ⇥ clarity ⇥ flags
    CHOSEN = os.path.join(OUT, f"{c}chosen.tsv")              # word ⇥ sentence  (hand-editable)
    TRANSLATIONS = os.path.join(OUT, f"{c}translations.tsv")  # word ⇥ russian
    WORDAUDIO = os.path.join(OUT, f"{c}wordaudio.tsv")        # word ⇥ filename | -
    REPORT = os.path.join(OUT, f"{c}check-report.txt")


def locate(word):
    """(list name, rank) for a word — which list built it, and at what rank.

    The media file name carries the rank, so anything that regenerates a file after the
    fact (fix_audit.py) has to know which list the word came from rather than assume.
    """
    for name in LISTS:
        for i, (w, _) in enumerate(words_of(name, 10 ** 9), 1):
            if w == word:
                return name, i
    return None, None


def words_of(name, n):
    """The first n (word, gloss) of one list, without switching the module over to it."""
    path = os.path.join(ROOT, LISTS[name]["file"])
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if p and p[0]:
                out.append((p[0], p[1] if len(p) > 1 else ""))
            if len(out) == n:
                break
    return out
DECK = "Frequency 🇩🇪"
STEM = "frequency_de"
NOTETYPE = "🇩🇪 German"
MODEL = "opus"
PER_WORD = 3
GROUP = 50            # words per sentence request
SCORE_GROUP = 150     # candidates per clarity request
TRANSLATE_GROUP = 250
META = os.path.expanduser("~/Library/Application Support/Anki2/addons21/MvJ Japanese/meta.json")
UA = "gigaku-mvj/1.0 (personal Anki add-on)"
MIN_MP3 = 2000        # anything shorter is a truncated reply, not a spoken sentence
DOWNLOAD_PACE = float(os.environ.get("COMMONS_PACE", "10"))   # seconds between file downloads
# German separable prefixes. One list, used by `flags` (a stranded prefix is the target's
# own, not another word) and by audit.py (a split verb is why a lemmatiser cannot find the
# target in the sentence). Written down rather than derived: no lemmatiser here can tell
# `an` in "Mach das Licht an" from the preposition.
PREFIXES = ("ab", "an", "auf", "aus", "bei", "ein", "mit", "nach", "vor", "zu", "zurück",
            "weg", "her", "hin", "los", "um", "durch", "über", "unter", "fest", "frei",
            "statt", "teil", "rein", "raus", "runter", "rauf", "weiter", "zusammen")

# Rows of de-words.tsv that are not words — a prefix entry the list-builder let through.
# Skipped at the apkg/media boundary only, so ranks (and so media file names) don't shift.
SKIP = {"unter-", "dar", "vorder"}   # prefixes and bound elements, not words:
# "dar" only in darstellen, "vorder" only in Vorder- compounds (measured by the audit)

# ── the rubric ──────────────────────────────────────────────────────────────────────
# One rule stated as a test (blank the target, then try its antonym — the same test
# lib/anki/prompt_de.py scores by, so the generator and the judge agree on what "clear"
# means), everything else a consequence. Worked examples are checked line by line: the
# gloss rubric's lesson is that an example teaching the mistake outweighs a rule
# forbidding it. Measured on the first 50 words (2026-08-26): all 150 answers 7–14 words,
# no sentence used another target, one first name, clarity mean 67 with 63/150 at 70+.
SYSTEM = """\
You write German example sentences for a learner's flashcards.

The learner watches German films and series and knows roughly the 2,000 most common
spoken German words. Each card teaches ONE word he does not know — the TARGET. He will
hear the sentence read aloud and see it written, with nothing else around it.

For each item you get an id, the TARGET in dictionary form and a Russian gloss listing its
senses. The gloss is a LIST, not a ranking: use the sense that is most common in everyday
spoken German — films, series, conversation — whichever position it has in the list, and
ignore rare, literary or technical senses entirely. Write THREE different sentences per
item, all in that one sense.

## The one test

Blank the TARGET out of your sentence and read what is left. How many different words could
fill that slot and still make a natural, sensible sentence? If any noun / verb / adjective
of that kind fits, the sentence has said nothing about THIS word — rewrite it. Then try the
target's opposite in the slot (links/rechts, schmutzig/sauber, ablehnen/annehmen): if the
opposite reads just as naturally, the sentence has only named the dimension. The rest of
the sentence must make the meaning concrete: what is done with the thing, where it is,
why, what follows from it, a contrast (nicht ..., sondern ...), a consequence, a reaction.

## What the sentence must be

- **Spoken.** A line a person would actually say — in a scene, at a kitchen table, on the
  phone. Vary the shape across the three: a question, an imperative, first person, a
  reaction to something just said — not three statements all beginning with Der/Das/Er.
  Not written prose, not a textbook example, and never a definition or an explanation
  of the word (no "X ist, wenn ...", no "man nennt das ...").
- **Built from words he knows.** Every word other than the TARGET must be among the
  ~1,500 most common spoken German words. When in doubt, use the plainer word. No other
  rare word, no other compound, no idiom, no slang he might not know. No proper names
  beyond a plain first name when the sentence needs one. The list of OTHER WORDS HE DOES
  NOT KNOW at the end of the request are all unknown to him — never use one of them
  inside another word's sentence.
- **7 to 14 words**, one sentence. A second clause after a comma is fine; a second
  sentence is not.
- **The TARGET appears exactly once and exactly as it is given — the same letters.**
  Not an inflected form: not `gewachsen` for `wachsen`, not `Fallen` for `Falle`, not
  `vernünftiger` for `vernünftig`, and a separable verb must not split (`anfassen`, never
  `fass … an`). German gives you this naturally almost every time: an infinitive after a
  modal, after `zu` or in the future (`Ich will dich nicht betrügen`), a noun in the
  nominative or accusative singular (`Das war eine Falle`), a predicative adjective
  (`Sei vernünftig`). Only the sentence's first letter may be capitalised. If the one
  natural sentence you have in mind needs another form, pick a different situation —
  never bend the German to fit the form. Do not put the target's synonyms, its opposite
  as a gloss, or its own parts elsewhere in the sentence.
- **Natural, contemporary, standard German.** No regionalisms, no forced formality. Read
  each line as a native speaker: if it sounds like a sentence written to contain a word,
  it is not finished.
- **Three genuinely different situations** per item, not one sentence reworded.

## Examples

TARGET: betrügen (обманывать, изменить)
- Sie hat ihn mit seinem besten Freund betrogen, jetzt will er sie nie wiedersehen.
  → the situation (with his best friend; he never wants to see her again) forces the sense.
- Der Typ hat mich beim Kartenspielen betrogen, ich hab alles verloren.
  → cheating at cards, lost everything: forced.
- Er hat sie betrogen.  ✗ — blank it: verlassen, geliebt, gesehen all fit. Says nothing.

TARGET: Koffer (чемодан)
- Ich hab den Koffer schon gepackt, wir fliegen morgen um sechs.
  → packing before a flight narrows it to luggage; and it is a line people say.
- Ein Koffer ist eine Tasche, in die man Sachen für eine Reise packt.  ✗ — a definition.

TARGET: wachsen (расти)
- Die Kinder wachsen so schnell, die Hosen von letztem Jahr passen schon nicht mehr.
  → the word stands exactly as given.
- Der Kleine ist so schnell gewachsen, seine Hosen sind alle zu kurz.  ✗ — same meaning,
  but the card says `wachsen` and the sentence says `gewachsen`.

TARGET: merkwürdig (странный)
- Merkwürdig, dass er nicht anruft, sonst meldet er sich jeden Abend.
  → the contrast with "sonst jeden Abend" makes "strange" the only reading.
- Das ist wirklich merkwürdig.  ✗ — blank it: schön, teuer, wichtig all fit.

TARGET: Richter (судья)
- Der Richter hat ihn zu drei Jahren Gefängnis verurteilt.  ✗ — "verurteilt" is not a
  word he knows, and Gefängnis is rare too. → Der Richter sagt, er muss drei Jahre ins
  Gefängnis, und der Mann fängt an zu weinen. ✓ (Gefängnis is common in films.)

TARGET: Gericht (суд, блюдо)
- Wir sehen uns vor Gericht, ich lasse mir das nicht gefallen.  ✓ — "court" is the
  sense a viewer meets; "dish" is the rarer one and gets no sentence.

## Output

One line per sentence, nothing else — no headings, no comments, no translations:

1/1: <sentence>
1/2: <sentence>
1/3: <sentence>
2/1: ...

Write every id exactly once with all three sentences. Plain text, German punctuation.
"""
FINGERPRINT = hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]

SYSTEM_RU = """\
Ты переводишь немецкие реплики на русский для обратной стороны карточки.

Каждая строка запроса — `id: TARGET | предложение`. TARGET — слово, которое карточка учит;
предложение — разговорная реплика из фильма или разговора. Ответь строкой `id: перевод`.

- Перевод естественный, разговорный, такой, как эту реплику сказали бы по-русски, — не
  подстрочник и не пересказ. Порядок слов и грамматика русские.
- Смысл TARGET в этом предложении должен быть передан точно и узнаваемо: это единственное,
  что читатель будет сверять. Если у слова несколько значений, переводится то, что стоит в
  предложении.
- Одно предложение — один перевод. Ничего не добавлять и не опускать, без пояснений,
  без скобок, без кавычек вокруг перевода.
- Только строки `id: перевод`, по одной на предложение, больше ничего.
"""


def note(msg):
    print(msg, file=sys.stderr, flush=True)


use_list(LIST)   # importable with the default list already bound


# ── data ────────────────────────────────────────────────────────────────────────────
def known_words():
    """The learner's known German words, lowercased — `gigaku words`, cached in out/."""
    if not os.path.exists(KNOWN):
        import subprocess
        done = subprocess.run(["gigaku", "words", "--lang", "de", "--stage", "known"],
                              capture_output=True, text=True, check=True)
        os.makedirs(OUT, exist_ok=True)
        with open(KNOWN, "w", encoding="utf-8") as f:
            f.write("".join(line.split("\t")[0] + "\n" for line in done.stdout.splitlines() if line.strip()))
    return {line.strip().lower() for line in open(KNOWN, encoding="utf-8") if line.strip()}


def words(n):
    """The first n rows of the active list as (word, gloss)."""
    return words_of(LIST, n)


def read_tsv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [line.rstrip("\n").split("\t") for line in f if line.strip()]


def append_tsv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write("\t".join(str(c) for c in row) + "\n")


# ── sentences ───────────────────────────────────────────────────────────────────────
_LINE = re.compile(r"^\s*(\d+)\s*[/.]\s*(\d)\s*[:.]\s*(\S.*?)\s*$", re.M)


def parse_sentences(text, batch):
    """`i/k: sentence` → {(word, k): sentence}; only ids asked for, first answer wins."""
    out = {}
    for m in _LINE.finditer(text or ""):
        i, k, sentence = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= i <= len(batch) and 1 <= k <= PER_WORD:
            out.setdefault((batch[i - 1][0], k), sentence)
    return out


def _sentence_request(batch, all_targets, attempt=0):
    mine = {w for w, _ in batch}
    others = [w for w, _ in all_targets if w not in mine]
    again = ("\n\nEvery sentence you sent for these words used an INFLECTED form. Write the "
             "target exactly as it is given, letter for letter — an infinitive after a modal "
             "or `zu`, a noun in the nominative or accusative singular, a predicative "
             "adjective. Change the situation if you must.\n" if attempt else "")
    return (f"{len(batch)} items, {PER_WORD} sentences each — answer with "
            f"{len(batch) * PER_WORD} lines.{again}\n\n"
            + "\n".join(f"{i}: {w} ({g})" if g else f"{i}: {w}"
                        for i, (w, g) in enumerate(batch, 1))
            + "\n\nOTHER WORDS HE DOES NOT KNOW (never use them): " + ", ".join(others))


def stage_sentences(n, effort):
    rows = read_tsv(CANDIDATES)
    have = {(r[0], int(r[1])) for r in rows}
    forms = {r[0] for r in rows if has_exact_form(r[2], r[0])}
    all_targets = words(n)

    def complete(w):
        """Enough candidates, AND at least one that uses the word as the card shows it.

        The form rule is verified here rather than trusted: a word whose whole batch came
        back inflected is asked again, which is the only thing that makes the rubric's
        rule real (`lib/anki/prompt.py`'s lesson — a rule with no check is a hope)."""
        return all((w, k) in have for k in range(1, PER_WORD + 1)) and w in forms
    todo = [(w, g) for w, g in all_targets if not complete(w)]
    if not todo:
        note(f"sentences: all {n} words already have {PER_WORD} candidates")
        return
    groups = [todo[i:i + GROUP] for i in range(0, len(todo), GROUP)]
    note(f"sentences: {len(todo)} words in {len(groups)} request(s), rubric {FINGERPRINT}, "
         f"model {MODEL}/{effort}")
    for gi, batch in enumerate(groups, 1):
        for attempt in range(4):
            reply, usage, cost = ask(_sentence_request(batch, all_targets, attempt), SYSTEM, model=MODEL,
                                     what=f"sentence batch {gi}/{len(groups)}", effort=effort)
            got = parse_sentences(reply, batch)
            # A re-ask must not overwrite the first answers, so later candidates get
            # the next free slot rather than 1..3 again.
            fresh = []
            for (w, _k), sentence in got.items():
                slot = max([k for (ww, k) in have if ww == w] or [0]) + 1
                have.add((w, slot))
                fresh.append((w, slot, sentence))
                if has_exact_form(sentence, w):
                    forms.add(w)
            append_tsv(CANDIDATES, fresh)
            rows = fresh
            note(f"  batch {gi}/{len(groups)}: {len(rows)} sentences landed · {describe(usage)} · ${cost:.2f}")
            batch = [(w, g) for w, g in batch if not complete(w)]
            if not batch:
                break
            note(f"    re-asking {len(batch)} word(s) that came back short")
        else:
            note(f"    still missing after 3 asks: {[w for w, _ in batch]}")


# ── check ───────────────────────────────────────────────────────────────────────────
_TOKEN = re.compile(r"[A-Za-zÄÖÜäöüß]+")


def has_exact_form(sentence, word):
    """Does the sentence use the target in the very form the card's Word field shows?

    The user's rule (2026-08-28): the card shows `wachsen`, so the sentence must say
    `wachsen` — not `gewachsen`, not `Fallen` for `Falle`, not a split `fass … an`. Only
    the sentence's first letter may differ in case, since German capitalises there and
    that is the same word; anywhere else case is meaning (`Falle` the trap vs `falle` the
    verb), so it must match exactly.
    """
    for m in re.finditer(r"(?<![A-Za-zÄÖÜäöüß])" + re.escape(word) + r"(?![A-Za-zÄÖÜäöüß])",
                         sentence, re.IGNORECASE):
        if m.group(0) == word:
            return True
        if m.start() == 0 and m.group(0) == word[:1].upper() + word[1:]:
            return True
    return False


def flags(sentence, target, unknown, known, lemmatize):
    """Words the sentence uses that the learner does not know — by both witnesses.

    A token is flagged only if de-words.tsv lists it (the list whose job is to name what
    he does not know) AND no form of it is in the Migaku known set. Either witness alone
    was measured to be noise: the known set under-reports basics (absent ≠
    unknown — `seit`, `essen`, `fahren`, `ob` are simply ungraded), so its complement
    flagged half of every sentence; and de-words.tsv carries those same ungraded basics
    plus homographs (`einen` the verb, `Muss` the noun, `Mach`), so on its own it
    flagged `hab`, `muss`, `Tag`, `Arbeit`. Their intersection left 22 real flags in
    150 candidates — Gefängnis, Kleid, Strand, Feuerwehr, Mittwoch — which is what a
    reader would have flagged. Advisory: the pick prefers zero flags, the report shows
    them.
    """
    tl = target.lower()
    out = []
    for tok in _TOKEN.findall(sentence):
        low = tok.lower()
        forms = {low, lemmatize(tok).lower(), lemmatize(low).lower()}
        if forms & {tl, lemmatize(target).lower()}:
            continue
        # The target's own parts are not other words: its separable prefix, stranded at
        # the end of the clause (anmachen → "Mach das Licht an"), and its bare stem.
        if low in PREFIXES and tl.startswith(low):
            continue
        if tl.endswith(low) and len(low) <= 5:
            continue
        if forms & unknown and not forms & known:
            out.append(tok)
    return out


def stage_check(n):
    import simplemma
    from lib.anki import prompt, prompt_de

    lemmatize = lambda w: simplemma.lemmatize(w, lang="de")
    order = [w for w, _ in words(n)]
    unknown = {w.lower() for w, _ in words(10 ** 9)}
    known = known_words()
    wanted = set(order)
    cands = [(w, int(k), s) for w, k, s in read_tsv(CANDIDATES) if w in wanted]
    if not cands:
        note("check: no candidates — run `sentences` first")
        return
    scored = {(r[0], int(r[1])): int(r[2]) for r in read_tsv(SCORES)}
    unscored = [(w, k, s) for w, k, s in cands if (w, k) not in scored]
    if unscored:
        # Six-digit ids: prompt.parse anchors on \d{6,} so a score can never be read as an id.
        ids = {100000 + i: (w, k, s) for i, (w, k, s) in enumerate(unscored, 1)}
        items = sorted(((i, s, w) for i, (w, k, s) in ids.items()),
                       key=lambda t: (order.index(t[2]), t[0]))
        groups = prompt.groups(items, SCORE_GROUP)
        note(f"check: rating {len(items)} candidates in {len(groups)} request(s) with the "
             f"clarity rubric ({prompt_de.FINGERPRINT}) …")
        for gi, group in enumerate(groups, 1):
            reply, usage, cost = ask(prompt.render(group), prompt_de.SYSTEM, model=MODEL,
                                     what=f"clarity scoring {gi}/{len(groups)}", effort="high")
            got = prompt.parse(reply, [i for i, _, _ in group])
            rows = []
            for i, score in got.items():
                w, k, s = ids[i]
                rows.append((w, k, score, ",".join(flags(s, w, unknown, known, lemmatize))))
                scored[(w, k)] = score
            append_tsv(SCORES, rows)
            note(f"  {gi}/{len(groups)}: {len(got)}/{len(group)} scores · {describe(usage)} · ${cost:.2f}")
    flagged = {(r[0], int(r[1])): (r[3].split(",") if len(r) > 3 and r[3] else [])
               for r in read_tsv(SCORES)}

    chosen = {r[0]: r[1] for r in read_tsv(CHOSEN)}
    new, report = [], []
    for w in order:
        options = [(ww, k, s) for ww, k, s in cands if ww == w and (w, k) in scored]
        if not options:
            note(f"  {w}: nothing scored, skipped")
            continue
        if w not in chosen:
            # Form first: a sentence that shows the word in another form loses to any
            # sentence that shows it as the card does, however clear it is.
            best = min(options, key=lambda t: (not has_exact_form(t[2], w),
                                               len(flagged.get((w, t[1]), [])),
                                               -scored[(w, t[1])], len(t[2])))
            new.append((w, best[2]))
            chosen[w] = best[2]
        report.append(f"\n{w}")
        for ww, k, s in sorted(options, key=lambda c: c[1]):
            fl = flagged.get((w, k), [])
            mark = "▶" if s == chosen[w] else " "
            report.append(f"  {mark} {k} [{scored[(w, k)]:>3}]{(' ⚑' + ' '.join(fl)) if fl else ''}  {s}")
    append_tsv(CHOSEN, new)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    picks_flagged = [w for w in order if w in chosen and any(
        flagged.get((w, k)) for ww, k, s in cands if ww == w and s == chosen[w])]
    wrong_form = [w for w in order if w in chosen and not has_exact_form(chosen[w], w)]
    note(f"check: {len(new)} new pick(s), {len(chosen)} chosen; report → {REPORT}; "
         f"picks that still carry a flag: {len(picks_flagged)} {picks_flagged[:20]}; "
         f"picks not in the card's own form: {len(wrong_form)} {wrong_form[:20]}")


# ── translate ───────────────────────────────────────────────────────────────────────
_RU_LINE = re.compile(r"^\s*(\d+)\s*[:.]\s*(\S.*?)\s*$", re.M)


def stage_translate(n, effort="medium"):
    chosen = {r[0]: r[1] for r in read_tsv(CHOSEN)}
    have = {r[0] for r in read_tsv(TRANSLATIONS)}
    todo = [w for w, _ in words(n) if w in chosen and w not in have]
    if not todo:
        note(f"translate: nothing to do ({len(have)} translated)")
        return
    groups = [todo[i:i + TRANSLATE_GROUP] for i in range(0, len(todo), TRANSLATE_GROUP)]
    note(f"translate: {len(todo)} sentences in {len(groups)} request(s)")
    for gi, batch in enumerate(groups, 1):
        for attempt in range(3):
            text = "\n".join(f"{i}: {w} | {chosen[w]}" for i, w in enumerate(batch, 1))
            reply, usage, cost = ask(text, SYSTEM_RU, model=MODEL,
                                     what=f"translation batch {gi}/{len(groups)}", effort=effort)
            got = {}
            for m in _RU_LINE.finditer(reply or ""):
                i = int(m.group(1))
                if 1 <= i <= len(batch):
                    got.setdefault(batch[i - 1], m.group(2))
            append_tsv(TRANSLATIONS, [(w, t) for w, t in got.items()])
            have |= set(got)
            note(f"  {gi}/{len(groups)}: {len(got)}/{len(batch)} · {describe(usage)} · ${cost:.2f}")
            batch = [w for w in batch if w not in have]
            if not batch:
                break
            note(f"    re-asking {len(batch)}")


# ── tts ─────────────────────────────────────────────────────────────────────────────
def ascii_slug(word):
    folded = (word.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
              .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue"))
    folded = unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode()
    # Lowercase, and that is measured: Anki's media store lowercased every one of these
    # on the way in (freq_de_001_Macht.mp3 → …_macht.mp3) while the notes kept the
    # capital — silent on this case-insensitive disk, a missing file after a sync.
    return re.sub(r"[^a-z0-9]+", "", folded.lower()) or "x"


def media_name(rank, word, prefix=None):
    return f"{prefix or PREFIX}_{rank:03d}_{ascii_slug(word)}.mp3"


def openai_key():
    with open(META, encoding="utf-8") as f:
        meta = json.load(f)
    key = (meta.get("config", {}).get("ai", {}).get("openai_key") or "").strip()
    if not key:
        sys.exit("no OpenAI key in the MvJ add-on's meta.json (Settings → AI Model)")
    return key


def tts(text, key):
    """mvj/services/tts.py::_request_openai_tts, verbatim in its parameters."""
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps({"model": "tts-1", "voice": "shimmer", "input": text,
                         "speed": 1.0}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    # Retried, because a 500-file run met one reset connection at file 116 and lost the
    # rest of the stage to it (2026-08-26) — the cache made the re-run free, the hour was not.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 3:
                raise
        time.sleep(5 * (attempt + 1))


def stage_tts(n):
    chosen = {r[0]: r[1] for r in read_tsv(CHOSEN)}
    os.makedirs(MEDIA, exist_ok=True)
    key = None
    made = 0
    for rank, (w, _) in enumerate(words(n), 1):
        if w not in chosen:
            continue
        path = os.path.join(MEDIA, media_name(rank, w))
        if os.path.exists(path):
            continue
        key = key or openai_key()
        data = tts(chosen[w], key)
        if not data.startswith(b"ID3") and data[:2] != b"\xff\xfb" and data[:2] != b"\xff\xf3":
            sys.exit(f"tts for {w}: reply is not mp3 ({data[:40]!r})")
        if len(data) < MIN_MP3:
            sys.exit(f"tts for {w}: {len(data)} bytes is a truncated answer, not speech")
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
        made += 1
        note(f"  {os.path.basename(path)}  {len(data):,} B  «{chosen[w]}»")
    note(f"tts: {made} new file(s), {len(chosen)} total in {MEDIA}")


# ── wordaudio ───────────────────────────────────────────────────────────────────────
def _commons(url, tries=6):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Retry-After lies low: upload.wikimedia.org says "1" and then, hit
                # again a second later, bans the IP for 600 s (measured: 6 bans in an
                # hour, 22 files). Honour a long Retry-After, never a short one.
                wait = max(int(e.headers.get("Retry-After") or 0), 30 * (attempt + 1))
                note(f"    Commons 429 on {url.rsplit('/', 1)[-1]}, waiting {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Commons kept answering 429: {url}")


LOOKUP = os.path.join(OUT, "commons-lookup.json")   # candidate → url | "" (cached answers)

# `definitions.py` calls commons_lookup from four worker threads. The cache is a
# read-modify-write of one whole file, and the staging file used to be a single shared
# `.part` path, so two workers interleaved their JSON into it and os.replace published the
# result: one valid object followed by the tail of another. Every later json.load then
# raised, instantly, for every remaining word — measured 2026-09-04, 283 of 321 words lost
# that way while the run still reported "done". The lock makes the read-modify-write atomic;
# the per-writer temp name means no two writers can ever share a staging file again.
_LOOKUP_LOCK = threading.Lock()


def _lookup_load():
    """The cache, or {} if it is missing or damaged — a bad cache costs API calls, not a run."""
    if not os.path.exists(LOOKUP):
        return {}
    try:
        with open(LOOKUP, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        note(f"    commons cache at {LOOKUP} is unreadable — starting a fresh one")
        return {}


def _lookup_save(entries):
    """Merge `entries` into the cache under the lock, so concurrent writers cannot lose
    each other's answers (a whole-file write of a stale copy would drop them silently)."""
    with _LOOKUP_LOCK:
        cache = _lookup_load()
        cache.update(entries)
        tmp = f"{LOOKUP}.{os.getpid()}.{threading.get_ident()}.part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, LOOKUP)


def commons_lookup(cands):
    """{candidate: download url} for the De-<candidate>.ogg files that exist, 50 titles a call.

    Answers are cached in commons-lookup.json: the API 429s at one call a second
    (Retry-After ~40 s), so a restarted run must not pay for the 40 lookups again."""
    with _LOOKUP_LOCK:
        cache = _lookup_load()
    found = {c: cache[c] for c in cands if cache.get(c)}
    cands = [c for c in cands if c not in cache]
    titles = [f"File:De-{c}.ogg" for c in cands]
    for i in range(0, len(titles), 50):
        q = urllib.parse.urlencode({"action": "query", "titles": "|".join(titles[i:i + 50]),
                                    "prop": "imageinfo", "iiprop": "url", "format": "json"})
        data = json.loads(_commons("https://commons.wikimedia.org/w/api.php?" + q))
        norm = {n["to"]: n["from"] for n in data.get("query", {}).get("normalized", [])}
        batch = cands[i:i + 50]
        for page in data.get("query", {}).get("pages", {}).values():
            if "missing" in page or not page.get("imageinfo"):
                continue
            title = norm.get(page["title"], page["title"])
            found[title[len("File:De-"):-len(".ogg")]] = page["imageinfo"][0]["url"]
        _lookup_save({c: found.get(c, "") for c in batch})
        time.sleep(5)   # at 1 s the API answered 429 (Retry-After ~40 s) on the second call
    return found


def stage_wordaudio(n):
    """De-<Wort>.ogg from Commons for every word that has one; the add-on's casing ladder
    (word as written, then first letter flipped), the add-on's file name (lowercased, as
    Anki stores it), downloaded once and cached beside the mp3s."""
    os.makedirs(MEDIA, exist_ok=True)
    have = {r[0]: r[1] for r in read_tsv(WORDAUDIO)}
    todo = [w for w, _ in words(n) if w not in have]
    if not todo:
        note(f"wordaudio: nothing to do ({sum(1 for v in have.values() if v != '-')} recordings)")
        return
    cands = []
    for w in todo:
        cands += [w, w[0].swapcase() + w[1:]]
    found = commons_lookup(cands)
    hits = misses = 0
    for i, w in enumerate(todo, 1):
        cand = next((c for c in (w, w[0].swapcase() + w[1:]) if c in found), None)
        if not cand:
            append_tsv(WORDAUDIO, [(w, "-")])   # written per word: a killed run keeps its work
            misses += 1
            continue
        name = f"de-{cand}.ogg".lower()
        path = os.path.join(MEDIA, name)
        if not os.path.exists(path):
            data = _commons(found[cand].split("?")[0])
            if not data.startswith(b"OggS"):
                note(f"  {w}: Commons answered something that is not ogg, skipped")
                append_tsv(WORDAUDIO, [(w, "-")])
                misses += 1
                continue
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            # Measured 2026-08-26: at 1 s between downloads upload.wikimedia.org answers
            # 429 (Retry-After: 1) on most files and then bans the IP for 600 s; at 3 s
            # it still banned every third file — 20 files an hour. DOWNLOAD_PACE is the
            # knob; 10 s is where the run is being watched from.
            time.sleep(DOWNLOAD_PACE)
        append_tsv(WORDAUDIO, [(w, name)])
        hits += 1
        if i % 50 == 0:
            note(f"  {i}/{len(todo)} words, {hits} recordings so far")
    note(f"wordaudio: {hits} recording(s) for {len(todo)} word(s), "
         f"{misses} with no recording on Commons (the 💣's TTS covers those)")


# ── apkg ────────────────────────────────────────────────────────────────────────────
FIELDS = ["Sentence", "am-study-morphs", "Word", "Word Audio", "Definition", "Notes",
          "Sentence Audio", "Definition Audio", "am-all-morphs-count", "My Alternatives",
          "My Clarity", "My Run", "Image", "Context", "My Sort Key", "am-all-morphs"]


def stage_apkg(n):
    import genanki

    chosen = {r[0]: r[1] for r in read_tsv(CHOSEN)}
    translations = {r[0]: r[1] for r in read_tsv(TRANSLATIONS)}
    wordaudio = {r[0]: r[1] for r in read_tsv(WORDAUDIO) if r[1] != "-"}
    model = genanki.Model(
        int(hashlib.md5((NOTETYPE + "_model").encode()).hexdigest()[:8], 16),
        NOTETYPE,
        fields=[{"name": name} for name in FIELDS],
        templates=[{"name": "Listening Card", "qfmt": "{{Sentence Audio}}",
                    "afmt": "{{FrontSide}}<hr id=answer>{{Sentence}}"}],
        css=".card { font-family: sans-serif; font-size: 24px; text-align: center; }",
    )
    deck = genanki.Deck(int(hashlib.md5(DECK.encode()).hexdigest()[:8], 16), DECK)
    missing, untranslated = [], []
    for rank, (w, _) in enumerate(words(n), 1):
        name = media_name(rank, w)
        if w in SKIP:
            continue
        if w not in chosen or not os.path.exists(os.path.join(MEDIA, name)):
            missing.append(w)
            continue
        values = dict.fromkeys(FIELDS, "")
        values["Sentence"] = chosen[w]
        values["Word"] = w
        values["Sentence Audio"] = f"[sound:{name}]"
        if w in translations:
            values["Notes"] = translations[w]
        else:
            untranslated.append(w)
        if w in wordaudio and os.path.exists(os.path.join(MEDIA, wordaudio[w])):
            values["Word Audio"] = f"[sound:{wordaudio[w]}]"
        deck.add_note(genanki.Note(model=model, fields=[values[f] for f in FIELDS],
                                   guid=genanki.guid_for(f"freq-de:{w}")))
    out = os.path.join(OUT, f"{STEM}-{LIST}.apkg")
    package = genanki.Package(deck)
    package.media_files = []
    package.write_to_file(out)
    note(f"apkg: {len(deck.notes)} notes → {out}"
         + (f" (no sentence/audio, skipped: {missing})" if missing else "")
         + (f" (no translation yet: {len(untranslated)})" if untranslated else ""))


# ── media ───────────────────────────────────────────────────────────────────────────
def anki(action, **params):
    req = urllib.request.Request(
        "http://127.0.0.1:8765",
        data=json.dumps({"action": action, "version": 6, "params": params}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        answer = json.load(r)
    if answer.get("error"):
        raise RuntimeError(f"{action}: {answer['error']}")
    return answer["result"]


def stage_media(n):
    """Put every sentence mp3 and word ogg into collection.media over AnkiConnect.

    collection.media, not MvJ's database.media, and that is measured: the 🇩🇪 card
    template is the MvJ one, which plays [audio:] with its own JS from the webview, and
    the webview serves collection.media only — a file in database.media is what the
    browser's Z can play and the reviewer cannot. The podcast clips live in
    database.media because a database note is not reviewed until 💾 fetches it up;
    this deck is reviewed where it lands. (The post-import unfetch pass is scoped to
    database decks since 2026-08-26 so it cannot sweep these out again.)
    """
    chosen = {r[0]: r[1] for r in read_tsv(CHOSEN)}
    wordaudio = {r[0]: r[1] for r in read_tsv(WORDAUDIO) if r[1] != "-"}
    anki("version")
    collection_media = os.path.expanduser(
        "~/Library/Application Support/Anki2/MainProfile/collection.media")
    pushed = skipped = 0
    for rank, (w, _) in enumerate(words(n), 1):
        if w not in chosen or w in SKIP:
            continue
        for name in (media_name(rank, w), wordaudio.get(w)):
            if not name:
                continue
            path = os.path.join(MEDIA, name)
            if not os.path.exists(path):
                note(f"  {name}: not generated yet")
                continue
            # Compared by CONTENT, not by name. A re-generated sentence keeps its file
            # name (same word, same rank), so a name-based skip left the new audio on
            # disk and the old audio in the collection — measured 2026-08-28, 630
            # re-recorded sentences silently did not reach Anki.
            there = os.path.join(collection_media, name)
            if os.path.exists(there) and os.path.getsize(there) == os.path.getsize(path):
                with open(there, "rb") as a, open(path, "rb") as b:
                    if a.read() == b.read():
                        skipped += 1
                        continue
            got = anki("storeMediaFile", filename=name, path=path)
            pushed += 1
            if got != name:
                note(f"  {name}: Anki stored it as {got}")
    note(f"media: {pushed} file(s) pushed into collection.media, {skipped} already identical")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["sentences", "check", "translate", "tts", "wordaudio",
                                      "apkg", "media", "all"])
    ap.add_argument("--n", type=int, default=50, help="how many words from the top of the list")
    ap.add_argument("--list", default="words", choices=sorted(LISTS),
                    help="which source list to build from (see LISTS)")
    ap.add_argument("--effort", default="high", help="claude --effort for the sentence request")
    a = ap.parse_args()
    use_list(a.list)
    os.makedirs(OUT, exist_ok=True)
    note(f"list {a.list}: {WORDS}")
    if a.stage in ("sentences", "all"):
        stage_sentences(a.n, a.effort)
    if a.stage in ("check", "all"):
        stage_check(a.n)
    if a.stage in ("translate", "all"):
        stage_translate(a.n)
    if a.stage in ("tts", "all"):
        stage_tts(a.n)
    if a.stage in ("wordaudio", "all"):
        stage_wordaudio(a.n)
    if a.stage in ("apkg", "all"):
        stage_apkg(a.n)
    if a.stage == "media":
        stage_media(a.n)


if __name__ == "__main__":
    main()
