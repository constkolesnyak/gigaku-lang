# `freqdeck/` — the `Frequency 🇩🇪` deck

Takes the first N words of a ranked list (`freq/de-words.tsv` — spoken German in general —
or `freq/de-yt-deck.tsv`, the user's own subtitle corpus) and turns them into Anki notes on
the `🇩🇪 German` note type: the same note type the sentence-mined decks use, except that the
example sentence and its audio are *generated* — the sentence by Opus through `claude -p`
(`lib/claude.py`), the audio by TTS — rather than cut out of a recording.

Built 2026-08-28: **1,997 notes** — 998 from the general frequency list and 999 from the
subtitle list. Every field filled on every note, no German-language definitions, the whole
deck's audio at one level. The final review of the whole deck after the last rework: 1,951
clean on the first pass, 47 findings fixed and re-reviewed down to zero (7 of them by hand).

## What is in a note

| field | where it comes from |
|---|---|
| `Sentence` | Opus, by the rubric in `deck.SYSTEM`: 3 candidates per word, the best chosen by `check` |
| `Sentence Audio` | OpenAI `tts-1`/`shimmer` — the same voice the add-on uses for definitions |
| `Word` | the word from the list |
| `Word Audio` | a native recording from Wikimedia Commons (`De-<Wort>.ogg`); TTS where there is none |
| `Definition` / `Definition Audio` | an English definition by the add-on's own prompt (gpt-4o) + its TTS |
| `Notes` | a Russian translation of the sentence (Opus) — the field the add-on's own translation goes into |
| `am-*` | AnkiMorphs, after `R` |

There is no monolingual (German) definition, by the user's decision; the add-on's
`german_prompts.generate_text_monolingual_definition` flag is honoured as a veto.

## Building

```sh
# 1. the long stages — detached, so they outlive the shell
(nohup freqdeck/run.sh 1000 sentences check translate tts \
    > freqdeck/out/logs/build.log 2>&1 &)
(nohup env COMMONS_PACE=15 freqdeck/run.sh 1000 wordaudio \
    > freqdeck/out/logs/wordaudio.log 2>&1 &)

# 2. WAIT for wordaudio to finish, only then package (Anki must be running for `media`)
uv run --with genanki --with simplemma python freqdeck/deck.py apkg --n 1000
uv run --with genanki --with simplemma python freqdeck/deck.py media --n 1000

# 3. import — inside Anki, through oneshot_addon (below)
echo $PWD/freqdeck/anki_import.py > /tmp/gigaku_oneshot_script.txt

# 4. definitions, their TTS and Word Audio where empty — over AnkiConnect
uv run python freqdeck/definitions.py

# 5. queue order across the whole deck, recalc, and the closing check
echo $PWD/freqdeck/order.py > /tmp/gigaku_oneshot_script.txt
echo $PWD/freqdeck/recalc.py > /tmp/gigaku_oneshot_script.txt
uv run --with simplemma python freqdeck/audit.py          # mechanics + Opus review
uv run python freqdeck/fix_audit.py                       # apply the findings
uv run --with simplemma python freqdeck/audit.py --words a,b,c   # re-check those
```

`deck.py` runs one stage or `all`: `sentences → check → translate → tts → wordaudio →
apkg`, then `media` (with Anki running). `--list words|yt` picks the source list; `run.sh`
runs stages in sequence with a log and passes `LIST` and `COMMONS_PACE` through.

To extend the deck, raise `--n` and repeat: only the new words are asked for, a note's guid
is the word itself, so a re-import through the MvJ importer **updates** existing notes
(`preserve_guids`: a non-empty field in the apkg replaces, an empty one leaves the live
value) instead of duplicating them. Everything generated lives in `out/` (gitignored); every
stage skips what already exists, so a re-run after a failure is free.

`oneshot_addon/` is a disposable add-on for what can only be done inside Anki's process
(the MvJ importer, AnkiMorphs' recalc, repositioning cards): copy it to
`~/Library/Application Support/Anki2/addons21/zz_gigaku_oneshot`, restart Anki, and
**remove it when done**. Every 5 s it looks for `/tmp/gigaku_oneshot_script.txt`, removes
the trigger first (never a loop), and runs the named script on Anki's main thread.

## The second thousand comes from the user's own subtitles, not from the tail of the list

The deck builds from any ranked list: `deck.py --list <name>`. The first thousand is the
head of `freq/de-words.tsv` (spoken German in general); the second is
`freq/de-yt-deck.tsv`, the vocabulary of the channels the user actually watches. The
numbers behind that (the full comparison is in `freq/README.md`, *The subtitle list*): 877
of the first thousand occur in those subtitles, but at a median rank of #2670, and 123 are
never said at all (`Sarg`, `Pfarrer`, `Geschworene` — the film skew). Meanwhile 728 words
the subtitles put in their top 3,000 sit past #1000 in the frequency list: `teilweise` #70
against #1338, `generell` #97 against #3669, `abonnieren` #135 against #6133 — the
connectives of a spoken monologue, and YouTube vocabulary.

`wordlist.py` prepares such a list: it subtracts **`ungraded` as known** (the user's
decision, 2026-08-27 — 2,409 rows, 438 of the top 500; otherwise the first cards teach
`ob`), the known words, and what the deck already has, then lets Opus filter out English
intrusions, names, abbreviations and bare forms in one pass, giving a Russian gloss as it
goes. Every rejection with its reason is in `out/yt-verdicts.tsv`, to be read by eye: a
filter is checked by what it removed.

## Card order: one queue for the whole deck

One deck, several lists — and the second list must not simply sit behind the first, or
`teilweise` (177/M in the user's own subtitles) waits behind `Truthahn`. `order.py` (run
inside Anki through `oneshot_addon`, because repositioning new cards is a scheduler call,
not something AnkiConnect exposes) computes the order and repositions **only new** cards;
the previous positions go to `out/order-before.json`, so the rollback is exact.

**The score is expected exposure, so the corpora are SUMMED, not multiplied.** The user
watches both films and YouTube, so a word reaches them at its rate in one plus its rate in
the other. The geometric mean was tried first and is wrong here: absence from a corpus is a
zero that kills the product, and absence is honest on both sides — the YouTube corpus is
~6 M tokens and a word can miss it by chance (`wachsen`, second by general frequency, is
unattested there and sank to **#1546**); the general list subtracts what Migaku knows and
what the personal filter cut (`streamen` sank to **#1592**). Summed, they land at **#18**
and **#95**, where a person would put them. Each corpus is divided by its own median over
the deck's words, because `living_score` and "per million in the subtitles" are otherwise
not comparable.

**One correction, measured:** a YouTube rate is trustworthy only as far as it is spread
across channels. `Einatmen` reads 49.7/M off **8** channels and `Ausatmen` 47.1/M off
**4** — yoga videos repeating one instruction, not vocabulary met everywhere. So the YouTube
term is damped by `min(1, channels / 25)`: those two move to #826 and #1674, and everything
genuinely spread stays put (`teilweise` — 216 channels, `Staffel` — 75, still #2 and #3).

**And the order has to be defended against recalc.** Measured 2026-08-28: AnkiMorphs
rewrites the `due` of every new card it manages on `R` — the queue came back as `oha,
Rubel, Mod, …`, so a one-shot reposition lasts until the next recalc (its own
`recalc_move_new_cards_to_the_end` / `recalc_offset_new_cards` switches are off and
irrelevant: placing new cards *is* recalc's job). So `order.py` **stores the queue in the
collection** (`gigaku.freqOrder`: the deck and its card ids in order) and the add-on's
`features/freq_order.py` replays it after every recalc — computing nothing, just repeating
the list; cards that are no longer new drop out of it (`rules.freq_order_cards`). The
off-switch is `"freq_order": {"enabled": false}`, or clearing the key. Checked on the live
collection: after `R` the queue is unchanged.

The result over 2,000 words: the head is `empfehlen, teilweise, Staffel, generell,
Hintergrund, Gericht, abonnieren, Macht`; the film vocabulary sinks (`Sarg` #852, `Kaution`
#1781); new words are introduced gradually rather than in a block at the end — by quarter of
the deck, 109 / 213 / 247 / 431.

## The word appears in the sentence in the card's form

The user's requirement (2026-08-28): if the card shows `wachsen`, the sentence says
`wachsen`, not `gewachsen`. Measured before the fix: 639 of 1,998 notes broke it.

The rule lives in three places, and only together do they work. **In the rubric** — a point
with a counter-example (`Der Kleine ist so schnell gewachsen` ✗), because an example weighs
more than a rule here. **In the code** — `has_exact_form`: a word counts as done only if at
least one candidate carries the exact form, and in the pick a candidate without it loses to
any with it; a sentence-initial capital is the same form, but `Falle` and `falle` are
different words, so the case check is exact everywhere except the first letter. **In the
re-ask** — a batch that came back entirely in oblique forms is asked again with an explicit
instruction.

**What the rule cost, and why grammar now outranks it.** The constraint bent German where
the dictionary form does not occur in speech: `restlich` and `fehlend` exist only before a
noun, `vorder` only inside a compound (`Vorderrad`), `ungut` only in `nichts für ungut`.
The model obediently wrote predicative constructions nobody says, and the audit caught them.
`fix_audit` now says outright: if the dictionary form is impossible, write naturally. Nine
cards keep an oblique form, all of that class (`damalig`, `jeweilig`, `bisherig`, `sonstig`,
`vorherig`, `restlich`, `fehlend`, plus `Kopfschmerz`/`Bauchschmerz`, which live in the
plural). `vorder` and `dar` were removed from the deck altogether — bound parts of words,
not words.

**A consequence found twice:** the definition is derived from the sentence, so a rewritten
sentence leaves a definition describing text that is gone (`stürzen`: was "to fall", became
"to overthrow"; then `zustoßen`, `Maler`). `definitions.py --redo` clears the definitions of
the named words, and `fix_audit` regenerates them right after rewriting a sentence.

## What was measured (and what it cost)

**The sentence rubric** (`deck.SYSTEM`) is built on one test: blank the target word and ask
how many words could fill the hole; then substitute the antonym — if the antonym sounds just
as natural, the sentence named only the dimension, not the word. Everything else is a
consequence: a spoken line (not prose, not a definition of the "X ist, wenn …" kind), 7–14
words, frequent words only, the target exactly once, three different situations. Every
request carries **the whole** list of targets as "words the learner does not know", or the
sentence for word 20 leans on word 300. The dictionary gloss is **a list, not a ranking**:
the sense that is frequent in film and conversation is taken (`Gericht` = court, not dish).

**The pick among three** uses the clarity rubric `lib/anki/prompt_de.py` — the same one that
scores i+1 cards: a word's candidates are compared against each other in one block. Plus a
mechanical vocabulary check: a token is flagged only if it is in `de-words.tsv` **and** no
form of it is in the known words (`gigaku words --lang de --stage known`). Either witness
alone is noise: the known set does not know the basics (`seit`, `essen`, `ob` are simply
ungraded), and `de-words.tsv` carries those same basics plus homographs (`einen` the verb,
`Muss`, `Mach`). The intersection gave 22 real flags on 150 candidates instead of half of
every sentence.

**Opus's answers are bimodal** (recorded in `lib/claude.py` too): the same request comes back
at either ~5k or ~20k output tokens, and the "fast" mode is consistently 2–4 clarity points
worse. Best-of-three smooths it out — over 1,000 words the mean score of the chosen
sentences was 70.3 and 70.4 on the two halves.

**The card's audio lives in `collection.media`, not `database.media`.** The 🇩🇪 template is
the MvJ template; it plays `[audio:]` from its own JS in the webview, and the webview serves
only `collection.media`; `database.media` is what `Z` plays from in the browser. This is why
the add-on's post-import unfetch pass had to be restricted to Database decks — otherwise the
next German import would have carried this deck's audio out and the cards would have gone
silent.

**Media file names are lowercase.** Anki lowercases what it stores
(`freq_de_001_Macht.mp3` → `…_macht.mp3`) while the note keeps the capital: invisible on
this case-insensitive disk, a missing file after a sync to a case-sensitive one.

**Commons limits by count, not pace**: ~12–16 requests, then a 600 s ban — identically at
3, 10 and 15 s pauses. The ceiling is ~60 files an hour from one IP; 987 recordings took
16 hours. Hence `wordaudio` is a separate detached stage, API answers are cached
(`out/commons-lookup.json`), and results are written per word. **And the user's rule: no
temporary TTS "until Commons catches up"** — recordings run to completion first, then
everything else; a word is TTS'd only where Commons genuinely has nothing. The same limit
explains why the add-on's 💣 "could not find" recordings (37 "misses" out of 50 — all 50
exist).

**The Anki UI is not used any more.** The first 800 notes went through the add-on's 💣 from
the browser: modal dialogs (a Commons 429 inside the add-on hangs Anki until OK is clicked),
the main thread blocked 13–17 minutes per batch, and — in All-In-One mode — cards moved
into `Study 🇩🇪`. `definitions.py` does exactly the same over AnkiConnect: the same prompt
and system prompt from `german_prompts`, the same gpt-4o with the same parameters from
`meta.json`, the same `<!-- def-type="bilingual" -->…` block format, the same TTS and file
names — 4 workers, ~4 s per note, no dialogs and no moves.

**The whole-deck review** (`audit.py`, Opus, 100 notes per request, reading word + sentence
+ definition + translation together): 959 of 999 OK on the first pass. 40 findings — 20
translations (calques and lost meaning), 8 definitions with a German word inside ("Knast →
…" — a direct consequence of the add-on's prompt asking for the "core meaning, not this
sentence"), 7 wrong senses (`Verhandlung` explained as negotiations while the sentence is a
trial), 5 unidiomatic sentences. `fix_audit.py` re-asks each finding with the reviewer's
objection in the prompt and fixes **both the note and the caches** in `out/`, or the next
`apkg` would bring the old text back. Re-check of the fixed notes: 0 findings.

The mechanical check separately lists the 18 sentences where the lemmatiser cannot confirm
the target is present (separable verbs `Mach … an`, participles `abgebogen`, `U-Bahn`) —
a "read by eye" list, not an error list: all 18 read and fine.

**The `K` key** in Anki's main window opens the browser on `deck:"Frequency 🇩🇪"`
(`nav.goto_de_query` in the `anki/gigaku` add-on); an empty value restores the old
behaviour — the my-learn 🇩🇪 queue.

## Card styling

The card's CSS is not in this repo: it lives in the `🇩🇪 German` note type's `⚙ SETTINGS`
region inside Anki, which the add-on's template sync carries over verbatim while the rest
of the template is re-derived from the Japanese note type on every profile load. Two lessons
from tuning it for a phone, worth keeping: **AnkiDroid wraps the card in its own container**,
so a `body > .card-inner` rule matches on the Mac and silently never on the phone — write
`html .card …` and rely on nothing above `.card-inner`; and **nested selectors inside that
region do not survive on the phone** — close the `:root { … }` block, write flat rules, and
reopen it. Measure on the device, not on the desktop.

## Cost

Claude runs on the subscription through `claude -p`, so its real cost is $0 (the CLI prints a
"notional" ≈ $45 at API rates: sentences ≈ $19, clarity ≈ $15, translations ≈ $4, review
≈ $9 — a measure of volume, not a bill). Actually paid: OpenAI (gpt-4o + TTS) ≈ $6. The
deck's media: 132 MB in `collection.media`.

## Files

- `deck.py` — the stages `sentences | check | translate | tts | wordaudio | apkg | media` (or `all`).
- `wordlist.py` — a deck word list from a ranked source (subtract + filter + gloss) → `freq/de-yt-deck.tsv`.
- `definitions.py` — definitions, their TTS and Word Audio over AnkiConnect (the 💣 replacement).
- `loudness.py` — word recordings levelled to the TTS (see above).
- `order.py` — the queue order; `recalc.py` — AnkiMorphs recalc; `anki_import.py` — the import. All three run inside Anki through `oneshot_addon/`.
- `audit.py` / `fix_audit.py` — the closing check and applying its findings.
- `run.sh` — a detached run of stages with a log.
- `out/` — everything generated (candidates, scores, chosen sentences, translations, media, apkg, logs, audit reports); gitignored.
- Tests of the pure functions: `tests/test_freqdeck.py`.
