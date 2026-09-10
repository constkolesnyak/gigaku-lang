---
name: sentence-mining
description: Build German i+1 sentence-mining cards for Anki from any German YouTube video or playlist, using the user's Migaku known-words as the only definition of "known". Downloads audio only (no video, no screenshots), takes the transcript from YouTube's own `de-orig` captions (word-timed, free, no ASR key), diffs against Migaku, splits every unknown word three ways — teach it / grade it in Migaku / drop it — cuts the speaker's real voice out of the audio with ffmpeg, and pushes 🇩🇪 German notes. Use when the input is a German video URL or playlist plus any mention of Anki, cards, mining, i+1, or a deck. Trigger phrases: "mine this video", "make cards from <url>", "build a deck from this playlist", `/sentence-mining`.
---

# Sentence Mining — German

One mode: **a German YouTube URL (video or playlist) → 🇩🇪 German cards.**

```
  playlist / video URL
          │
   1  fetch.py            yt-dlp: bestaudio m4a  +  de-orig captions (json3, word-timed)
          │               split on German sentence punctuation → transcript.json
   2  analyze.py          spaCy lemmas ⟷ Migaku KNOWN  →  three fates per word
          │                  ├── known            → ignored
          │                  ├── common+ungraded  → grade-me.tsv   (marked, not taught)
          │                  └── unknown          → candidates.json
   3  triage.py           the words no frequency list vouches for → Opus: real German or noise?
   3b clarity.py          which of a word's sentences actually TEACHES it — and is there one
          │
   4  generate_media.py   ffmpeg cuts the sentence out of the m4a (the speaker's own voice)
   5  push.py             addNotes onto 🇩🇪 German — BARE: word, sentence, its audio, source
                          into `<parent>::<channel>`, one subdeck per channel
          │
          │  ── judge first, pay second ──
   6  recalc.py           AnkiMorphs' verdict; suspends what it already knows
                          …and, run again at the very end, syncs to AnkiWeb
   6b order.py            deck order = how often the word occurs IN THIS CHANNEL
   7  audit.py --apply    Opus reads word+sentence; suspends ASR corpses and dead context
          │
   8  translate.py        Russian translation of the sentence → `Notes` (the card's back)
   9  freqdeck/definitions.py --deck … --skip-suspended
                          Definition + Definition Audio + Word Audio, for SURVIVORS ONLY
  10  (levelling is inside step 9 — each recording is matched to the TTS as it lands)

**Judging comes before enrichment, and that ordering is the point.** Definitions (gpt-4o),
their TTS, and above all Commons word audio (~60 files/hour — hours for a deck) are the whole
cost of a build. A card that recalc or the audit is going to suspend must never be paid for.
The first build got this backwards — 400 cards enriched, then 48 suspended, 20 of them after
their word recording had already been downloaded. Steps 6–7 now run on bare cards, and step 8
takes `--skip-suspended`.

Everything below step 9 is **gigaku's existing German pipeline**, not this skill's: the cards
must sound and read like `Frequency 🇩🇪`, and the way to guarantee that is to let the same code
finish them.

## Run it

```bash
cd .claude/skills/sentence-mining
python3 scripts/check.py                       # 16 checks; do this first, it is 2 seconds
bash scripts/ensure_anki.sh

python3 scripts/fetch.py "<playlist-or-video-url>"
python3 scripts/analyze.py --dry               # see the split before it writes anything
python3 scripts/analyze.py
python3 scripts/triage.py                      # is the WORD real German? cached in triage.tsv
python3 scripts/clarity.py                     # which SENTENCE teaches it? cached in clarity.tsv
python3 scripts/generate_media.py
python3 scripts/push.py --dry                  # exactly what would be sent
python3 scripts/push.py
```

Then, after the definitions pass below has filled the cards:

```bash
python3 scripts/audit.py             # mechanical checks + an Opus read of every finished card
python3 scripts/audit.py --apply     # suspend + tag whatever it flagged (never deletes)
python3 scripts/recalc.py            # AnkiMorphs' verdict, and sweep what it already knows
python3 scripts/translate.py         # Russian sentence translation → Notes

# …then, once enrichment has finished, close the build:
python3 scripts/recalc.py --no-sync  # verdict + sweep
python3 scripts/order.py             # most frequent in this channel first, THEN sync
```

**`order.py` is the last step and it is the one that syncs.** recalc rewrites every new card's
due and the add-on replays the stored order ~1.5 s after recalc's operation lands, so a sync
fired at the end of recalc ships AnkiMorphs' order — the deck on the phone is then not the
deck on screen.

**A build that is not synced has not been delivered** — the studying happens on a phone, so
`recalc.py` ends with an AnkiWeb sync (`--no-sync` opts out). One sync at the end, never one
per file; Anki's own media sync then runs in the background inside Anki, and a first build is
~1,200 files / ~65 MB, so it keeps going for a few minutes after the script returns.

Then, from the repo root:

```bash
# fast: Definition (gpt-4o, MvJ's own 💣 prompts) + Definition Audio (tts-1/shimmer). Minutes.
uv run python freqdeck/definitions.py --deck "YouTube 🇩🇪" --no-word-audio

# slow: Word Audio from Wikimedia Commons, TTS only where Commons genuinely has nothing.
# ⛔ Commons limits by COUNT, not pace — ~60 files/hour whatever the delay — so this is HOURS
# for a 400-card deck. Detach it (double-fork, so it outlives the shell) and check
# back every couple of hours, not every five minutes. Re-running resumes.
uv run python freqdeck/definitions.py --deck "YouTube 🇩🇪"

# Word Audio is levelled AS IT DOWNLOADS — `definitions.py` runs each recording through
# loudness.process the moment it lands, so a file is never in the collection at the wrong
# level. Target = this deck's own Sentence/Definition Audio median, measured once per run.
# (`--no-normalize` opts out. Do not.)
uv run python freqdeck/loudness.py --deck "YouTube 🇩🇪"   # idempotent BACKSTOP, not the step
```

**Levelling is not a step you schedule.** It used to be a separate pass over the finished
deck, which could only run after a ~6-hour download — a step nobody is present for, and it was
duly forgotten once already. It now happens per file inside the download. Commons volunteers
sit ~7 dB above tts-1/shimmer (σ 2.88 vs 0.75), so without it every card jumps from a loud word
to a quiet sentence. The batch pass survives only as an idempotent backstop, and should report
`already` for everything.

Re-running any step is safe. `fetch.py` skips episodes already downloaded, `analyze.py`
recomputes from scratch, `triage.py` reuses `triage.tsv` and writes a
*separate* `triaged.json` (writing its capped result back over `candidates.json` once made a
second run start from the previous run's 400 instead of all 1,024, so re-running could only
shrink the deck), `generate_media.py` only clips what has no clip, and `push.py` asks
`canAddNotesWithErrorDetail` before sending.

## The three fates, and why there are three

This is the one idea in the skill. A word in the transcript is not simply known-or-unknown:

| fate | what it is | what happens |
|---|---|---|
| **known** | in Migaku, or its spaCy lemma is | ignored |
| **grade** | *not* in Migaku but in the top `grade_gate` (1500) of `freq/de-yt.tsv` | written to `grade-me.tsv`, **counted as known** for the i-level |
| **card** | not in Migaku, rarer than the gate, and a real word | a candidate |

Measured on episode 1 of the first playlist built (227 sentences, 2026-09-04): the raw i+1 list is
headed by `eigentlich` (#1 in spoken YouTube German), `bekommen` (#9), `sogar` (#13), `geil`
(#14), `fahren` (#19) — **118 such words against 49 genuinely new ones.** Making cards for
those would spend two thirds of the deck on A1 vocabulary; treating them as known would
break the user's own rule. They are neither: they are **bookkeeping gaps**, and the right
output for them is a list to mark in Migaku. Mark them, re-run, and they stop appearing.

Counting a grade-word as *unknown* when computing a sentence's i-level would push nearly
every sentence to i+3 and destroy the signal the whole method rests on — hence "counted as
known" above. Nothing is swallowed: every one is in `grade-me.tsv` with its rank, its
frequency, its Russian gloss and the sentence it was heard in.

`--gate N` overrides it for one run. Higher = more words sent to grading, fewer cards.

## The sentence is chosen by what it teaches

A word's sentence used to be picked by "fewest unknown words, then a length penalty". That
is a proxy for *difficulty*, not for *teaching*, and it selects exactly the card
`lib/anki/prompt_de.py` warns is the most common one in this collection: fluent, well-formed
and entirely empty. `Das macht man zurecht.` is short, clean, i+1 — and says nothing about
`zurecht`.

`clarity.py` uses the repo's own German clarity rubric unchanged — *blank the target out,
then ask how many words could fill the hole* — which is calibrated on this exact corpus
(transcribed conversational speech, ASR-split clauses, fillers) with anchors taken from real
cards in this collection. `prompt.render` lays a word's candidates side by side under one
heading, so the judgement is comparative rather than absolute.

For it to be able to choose, `analyze.py` offers **spans**, not just sentences: each sentence
alone, and glued to a neighbour either side, capped at ~230 chars / 16 s. YouTube's
punctuation cuts clauses short, and measured on this playlist half the words occur exactly
once — without spans there is nothing to choose between and the word is stuck with whatever
fragment it landed in. Neighbours are contiguous in the audio, so a span still clips as one
continuous cut.

And it rejects: when a word's best sentence is still below `--floor`, the honest outcome is
**no card**, not a bad card.

**Where the floor belongs: 50, and that is measured, not chosen.** The first build ran
at 60 and made 94 cards out of 1,097 candidates. Reading the rejected pool by score band shows
what the extra 10 points were actually buying:

| band | n | what is in it |
|---|---|---|
| 50–59 | 106 | real, useful German — `Hinsicht`, `beeindrucken`, `entwerfen`, `mithalten`, `Steigerung` |
| 40–49 | 128 | real German at the head (`platzieren`, `Wertschätzung`, `Hamsterrad`) mixed with ASR corpses — `Ar`, `arg`, `bang`, `ries`, `slice`, `schaubar` |
| ≤39 | 112 | mostly noise: `Straight`, `ass`, `Billo`, `Goatlist`, names |

The garbage starts at 50, not at 60, so a floor of 60 was throwing away 75 good cards to
exclude nothing. Re-running at 50 took the deck to **169**. Note the score does *not* measure
whether a word deserves a card — `platzieren` scores 44 and `Hauptcharakterplatz` scores 55;
that judgement is `triage.py`'s, and it deliberately keeps transparent compounds.

**What the floor is not responsible for.** Of the words rejected at 50 or above, 34 fail on
the 10-second clip ceiling alone, and they are real: `Knochen`, `opfern`, `bluten`, `klettern`,
`staunen`, `Wildschwein`, `Schicksalsschlag`. The ceiling is checked against `analyze.py`'s
padded estimate, so it is worth asking whether the real cut would fit — it was, and it does
not: run through `generate_media.clip()` with the ceiling lifted, the shortest of the 34 comes
out at exactly 10.0 s and the rest run over. That rejection is honest; do not go chasing it.

## What the frequency lists cannot decide

A word in neither `de-yt.tsv` nor `de-words.tsv` is not garbage. On episode 1 that bucket
held 201 words: anime titles (`Punchman`, `Isekai`), English used as-is (`Setup`, `Trigger`,
`hyped`), transcription corpses (`Einsteigeranim`, `Ohrwumm`, `hochwoten`) — **and real
German the lists simply lack** (`hochvoten`, `rausballern`, `wegdiskutieren`, `vollgeballern`,
`einpflegen`, `nachanimieren`, `Herzensding`, `Gesamtpaket`).

`analyze.py` drops what a rule can judge (a proper noun in this bucket is noise 51 times out
of 54; anything not shaped like a German word). `triage.py` sends the rest to Opus in groups
of 150 — one call, ~80 s, ~$0.6 — for five verdicts: keep, proper noun, foreign word, not a
word, or **inflected form**. That last one is not a bin: `musst`, `krasse`, `Herzen`, `weiter`
are real words in the wrong shape, so they are appended to `grade-me.tsv` rather than lost.

Verdicts are cached in `triage.tsv` by word, so re-running costs nothing and a hand-edit of
that file overrides the model.

## Two passes, judging two different things

`triage.py` judges the **word** — is this German, and is it worth a card. `audit.py` judges the
**card** — word, sentence and definition together, after the deck is built. They fail
independently, which is why both exist: `Maß` is a real German word with a real frequency rank,
so triage passed it, but the sentence it was mined from is YouTube mishearing "dieser **Mars**"
as "dieser Maß". Nothing about the word was wrong; the card was worthless.

`audit.py`'s rubric is deliberately *not* `freqdeck/audit.py`'s. That one reviews **generated**
sentences and treats unnatural or ungrammatical as a defect. These sentences are a YouTuber
talking fast, so colloquial, elliptical and ungrammatical is what correct input looks like here
— flagging it would flag most of the deck. Only four things count: the transcription is wrong
where it matters (`ASR`), the word is not really in its own example (`MISSING`), the definition
is of the wrong sense (`SENSE`), or the sentence says nothing without the video (`CONTEXT`).

`--apply` **suspends and tags** `sm-audit-<category>`; it never deletes. A suspended card is one
click from coming back, and every Anki-affecting step here has to be individually revertible.

## The i-level is AnkiMorphs' to say, not this skill's

`analyze.py` computes an i-level from the Migaku diff and uses it to pick the best sentence
for each word. It used to also **tag** the card `i1`/`i2`/`i3`, and that tag was wrong: measured
against a recalc on the first build, only **72 of 159** cards tagged `i1` were really i+1. The
mining side counts content words and treats grade-gate words as known; AnkiMorphs counts every
morph against its own known set. Two different questions, and the card carried the worse answer.

So no i-level tag is written any more. `recalc.py` runs at the end of a build and lets
AnkiMorphs write the real one — `_card-status::i+1` is the tag to study by — and suspends the
cards it calls `_card-status::i+0`, words already known that would otherwise be taught (8 of
400 on the first build). Nothing is deleted and no extra tag is added — `_card-status::i+0`
already says it, and AnkiMorphs keeps it current.

**This skill writes exactly one tag**, `claude-sentence-mining`. Channel and episode were
clutter (the seeked episode URL is in `Context`); the i-level is AnkiMorphs' to write. Only
`sm-audit-*` is kept beyond that, because why a card was suspended is recorded nowhere else.

`recalc.py` needs no human: AnkiMorphs' recalc must run on Anki's main thread, so the script
installs `freqdeck/oneshot_addon`, quits Anki, relaunches it, triggers the recalc, waits, and
removes the runner again. (AppleScript reports `User canceled` on this collection even when the
quit does proceed — the process list is the only thing worth believing.)

## One subdeck per channel

Cards go to **`<decks.parent>::<channel>`** — `YouTube 🇩🇪::<channel>` — with the channel taken
from what `fetch.py` recorded in the manifest, not from configuration. A channel is the unit
this skill actually works in: its own vocabulary, one speaker, one subject, and the frequency
the deck is ordered by is counted inside it. A flat deck would mix those and give you one
new-card limit across all of them.

Duplicate checking still runs against the whole tree, because Anki's `deck:"YouTube 🇩🇪"`
matches subdecks — so a word already carded from another channel is not carded again.
**That same rule is a trap on the other side**: a parent deck "contains" its subdecks' cards,
so any test of the form "does this deck still hold these cards" has to say
`deck:"X" -deck:"X::*"` or a parent will answer yes for cards that moved down into a child.

## The deck's order is the channel's frequency

`freq/de-yt.tsv` ranks spoken German across many channels — the right list for deciding
whether a word deserves a card at all, and the wrong one for deciding what to study first out
of *this* deck. These cards come from one person talking about one subject, so what they
say often is what turns up again next episode. `Hauptcharakter` occurs 32 times across this
playlist and `Backe` once, and no general list knows that.

`order.py` counts every content lemma across the deck's own transcripts and repositions the
new cards most-frequent-first.

⛔ **It also STORES the order.** AnkiMorphs' recalc rewrites the due of every new card it
manages, so a one-shot reposition lasts until the next **R**. The gigaku add-on replays
`gigaku.freqOrder` after each recalc — and that key used to hold exactly one deck, so writing
to it would have cost `Frequency 🇩🇪` its own replay. It now holds a list, old shape still
read, and both writers merge instead of overwriting. Verified: after a recalc the queue still
opens `Hauptcharakter (32×), Synchro (11×), Biest (6×)`.

## Never touch an open reviewer

This skill's checks once drove the user's own reviewer — switched the deck, called
`moveToState("review")` and `_showAnswer()` to measure a rendered card. It cost real work:
the Anki icon bounced in the Dock in the middle of someone else's session, and when a
rebuild deleted the deck the reviewer was left holding a dead card and Anki crashed with
`NotFoundError: No such card`, after which a Check Database was needed.

So: scripts that run inside Anki go to `deckBrowser` first, and `open` is called with `-g`
— launch without raising the window or taking focus. The one-shot runner stays installed so
Anki never has to be restarted at all: it listens for the trigger and picks it up on its
five-second tick.

## Traps

- **`[audio:…]`, never `[sound:…]`.** Since 2026-08-13 the 🇩🇪 note type renders MvJ's own
  template, whose front JS scans for `/\[audio:([^\]]+)\]/g`. `[sound:]` exists only inside an
  `.apkg`, rewritten by MvJ's importer on the way in. A note written with `[sound:]` is silent.
- **`Image` is never written.** Both templates wrap it in `{{#Image}}`, so empty renders
  nothing. Do *not* copy the Japanese skill's `。` filler — that existed to defeat a
  `{{^picture}}` branch this note type does not have, and would put a stray `。` on every card.
- **Media names are lowercase ASCII**, via `freqdeck.ascii_slug`. Anki's media store
  lowercases what it stores while the note keeps the capital: silent on this case-insensitive
  disk, a missing file after a sync.
- **The media prefix is `sm_de`, not `yt_de`** — `yt_de` already belongs to freqdeck's second
  source list, which is ~1,000 notes inside `Frequency 🇩🇪`.
- **Separable verbs are merged conservatively** (`legen … los` → `loslegen`). Three tests must
  all pass: the particle is ADV/PTKVZ and not ADP, it is clause-final, and prefix+lemma is a
  word the lists know. The first version had only the third test and minted `ausstellen` out
  of "Teamwork **aus** dem Fokus genommen … auf den Kopf **gestellt**" — a card for a word not
  in its own sentence. 8 merges in 227 sentences; missing one costs a card, inventing one
  costs trust.
- **Anki dedupes on the note type's FIRST field, which for 🇩🇪 is `Sentence`, not `Word`.** Two
  words whose best sentence is the same one cannot both become cards, and `addNotes` does not
  degrade gracefully — one unaddable note fails the *whole* batch with an error list (measured:
  395 good cards lost to 34 duplicates). So `analyze.py` keeps 6 runner-up sentences per lemma
  and hands each word a sentence of its own, and `push.py` pre-checks with
  `canAddNotesWithErrorDetail`.
- ⛔ **AnkiMorphs recalc owns new-card order.** A fresh deck is reordered by the next **R**;
  do not promise an order this skill cannot keep.
- **`gigaku words` is not the known set.** It calls `import_words()`, which rewrites the cache
  as a side effect of being read, and the cache cannot be filtered by source — 345 of its
  9,798 German words are AnkiMorphs/LR assertions, not Migaku. Read `migaku/de.csv`.
- **spaCy lives in AnkiMorphs' venv** and dies on Anki python bumps. `analyze.py` re-execs
  itself there; `check.py` is what tells you it is gone.

## Rollback

Everything lands in a deck that did not exist before and carries the tag
`claude-sentence-mining`. Nothing outside it is touched.

```bash
curl -s 127.0.0.1:8765 -d '{"action":"deleteDecks","version":6,
  "params":{"decks":["YouTube 🇩🇪::<channel>"],"cardsToo":true}}'
# then Tools ▸ Check Media to sweep the orphaned sm_de_* clips
```

## Files

| file | what |
|---|---|
| `config.json` | note type, field map, decks, the Migaku CSV, the frequency lists, the gate |
| `scripts/check.py` | 16 checks — Anki, fields, spaCy venv + model, backup freshness, tools, key |
| `scripts/fetch.py` | yt-dlp audio + `de-orig` captions → word-timed, sentence-split transcript |
| `scripts/analyze.py` | spaCy + Migaku diff + the three fates → `candidates.json`, `grade-me.tsv` |
| `scripts/triage.py` | Opus verdict on the words no list vouches for → `triage.tsv` |
| `scripts/generate_media.py` | ffmpeg sentence clips → `collection.media` → `draft.json` |
| `scripts/push.py` | `addNotes` onto 🇩🇪 German |
| `scripts/audit.py` | mechanical field/media checks + an Opus read of the finished card |
| `scripts/translate.py` | Russian translation of the sentence into `Notes`, freqdeck's own prompt |
| `scripts/recalc.py` | AnkiMorphs recalc without a human, then sweep what it already knows |
| `scripts/order.py` | rank the deck by channel frequency, reposition, store the order |
| `scripts/_oneshot.py` | install the runner, restart Anki, run a script on its main thread |
| `scripts/_german.py` | the spaCy interpreter, the known set, the frequency gate |
| `scripts/_config.py` `_anki.py` `ensure_anki.sh` | config, AnkiConnect, "is Anki up" |
| `references/known-words.md` | why Migaku alone, and how a token is matched against it |
| `references/note-type.md` | the 16 🇩🇪 fields and which ones this writes |

Work dir: `mining/` at the repo root — gitignored, and deliberately not `~/Downloads`.
