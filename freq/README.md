# `freq/` — a frequency list of spoken German

The word list behind the `Frequency 🇩🇪` deck (`freqdeck/`): colloquial German ranked by
how often it is *said*, lemmatised in context rather than counted as word forms, with the
words the user already knows (Migaku) subtracted and a Russian gloss on every row.

Built 2026-08-21, rebuilt 2026-08-24 after an audit (six defect classes, each measured and
closed — see *Audits* below).

## Files

Tracked:

| file | what |
|---|---|
| **`de-words.tsv`** | 19,793 words, `Wort<TAB>gloss`, in frequency order. The main result and `freqdeck/`'s input. |
| **`de-words-3k.tsv`** | the first 3,000 rows of the same file — the study core. |
| **`de-yt-deck.tsv`** | 1,000 words from the user's own YouTube subtitle corpus, prepared by `freqdeck/wordlist.py` from the untracked `de-yt.tsv` (see *The subtitle list* below). |

Generated, and kept out of the repo by `.gitignore` (everything under `freq/` is ignored
except the three lists, this file and the scripts):

| file | what |
|---|---|
| `de-living-german.tsv` | the list with its numbers: `rank, lemma, living_score, film_pm, everyday_pm, web_pm, film_skew, pos, wiktionary_pos` |
| `de-yt.tsv` | 15,256 words ranked over the user's own subtitle corpus (`rank, word, ru, pm, channels, cum, migaku`) — a separate list built on a different principle |
| `de-internationalisms.txt`, `de-known-forms.txt` | what the personal pass (`known.py`) cut, in frequency order |
| `de-ru.tsv`, `de-ru-fill.tsv` | dictionary glosses (de-wikt → ru-wikt) and the `claude -p` fill for the gaps; `ru.py` merges them into the gloss column |
| `de-spoken-cut.tsv` | 64,900+ rejected words, **each with its reason** — the filter can be checked rather than trusted |
| `de-spoken-final.tsv`, `de-freq-merged.tsv`, `report.txt`, `*.tsv.gz` | intermediates: the pre-blend ranking, the stage-one merge of seven public lists, their comparison, and the parsed corpora so ~2 GB of downloads need not be repeated |

Scripts, in pipeline order: `wikt.py` (the dictionary) → `ctx2.py` + `ctx_tat.py` (spaCy
over the corpora) → `fold7.py` → `polish2.py` → `blend.py` → `known.py` → `ru.py`
(+ `ru_fill.py` for the gaps). `foldlib.py` is the one fold shared by `fold7` and `blend`
— it used to exist as two copies with a comment promising they matched. `build.py` and
`analyze.py` are stage one: normalising the public lists onto one lemma axis and comparing
them with each other and with the user's known words.

## Why build one

Every ready-made "spoken" list is a list of **word forms**, not of text. That makes them
unusable for this purpose: `Zähnen` without its sentence cannot be told from the verb
`zähnen`, so the frequency of *teeth* migrates to a rare verb. The evidence — the sentence
— was thrown away before the list was made.

Of seven public lists checked, the best are OpenSubtitles and SUBTLEX-DE. They agree with
each other (Spearman 0.89) and disagree with news and Wikipedia (0.50–0.55): roughly a
third of the top 5,000 is different vocabulary. Spoken and written German really do
diverge, so the subtitle corpora are the right base.

## Sources

**Living (take part in the ranking):**

- **OpenSubtitles v2018 (OPUS)** — `object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/mono/de.txt.gz`,
  476 MB, 41.6 M lines. Every 16th line is taken; captions for the deaf and on-screen text
  are dropped (bracketed inserts like `(KICHERN)` and lines with no lowercase letters put
  `quietschen` at #836 and minted CAPS lemmas like ZIMT/REH) → **13,583,114 tokens**.
- **Tatoeba German** — `downloads.tatoeba.org/exports/per_language/deu/deu_sentences.tsv.bz2`,
  778,331 everyday sentences → **6,025,564 tokens**.

**Dictionary (lemmas, forms, spelling):**

- **German Wiktionary** via kaikki.org — `kaikki.org/dewiktionary/raw-wiktextract-data.jsonl.gz`
  (26 MB gz → ~3 GB JSONL; `lang_code=de` → 1,005,701 entries). Full coverage of
  declension/conjugation and of colloquial lemmas.
- **English Wiktionary, German entries** — `kaikki.org/dictionary/German/kaikki.org-dictionary-German.jsonl.gz`
  (96 MB; 369,987 entries). Machine tags the German edition lacks: `alt-of, archaic` on
  `laßt`, `Switzerland` on `fleissig`, `comparative` on `besser`.
- Both editions are needed: en writes some form pages as prose (`brachte` — "preterite of
  bringen", parsed out of the gloss under a "verb form" head), de knows the declensions
  (`bessere` → `gut` exists only there) and which plurals are separate words (`Eltern` yes,
  `Tage` no).

**Reference (filters and columns):**

- **DeReKo-2014-II** (IDS) — a named-entity column, to check proper nouns.
- **SUBTLEX-DE** (OSF, `osf.io/download/qang6/`) — an independent check.
- **Migaku** — 11,719 German rows read out of Chrome, subtracted whole (**any status**,
  by the user's instruction), including the expansion of word-form rows (`führt`, `führte`
  → führen) **by the row's part of speech** (below).

Not obtainable: **GeRedE** (270 M tokens of German Reddit — CQPweb only), **FOLK/DGD**
(real spontaneous speech — registration only).

## Pipeline

**1. A dictionary with classified senses** (`wikt.py`). A Wiktionary page is not a lemma;
what it *is* is written in its senses. A sense with `form_of`/`alt_of` is a form; an entry
whose senses are all form senses is a **form page** (edges, not a lemma); an entry with a
real sense is a **lemma**, and its form senses become `minor` edges — recorded, never
followed (an archaic link from `führen` to `fahren` does not outweigh six verb senses).
`alt_of` is a spelling claim: `old` (pre-reform) and `alt` (Swiss) edges are applied as
normalisation before any other logic. Derivation (`Anwältin` ← Anwalt, `Tänzer` ← tanzen,
`Stückchen` ← Stück) is not inflection: a derivational sense carries no case tag and
defines a word of its own. Comparatives get their own edge kind, `degree` — the one
semantic marker that separates `schlimmer` (Komparativ of schlimm) from `lange` (which has
only a declined-form page, the adverb being its own lexeme).

**2. Tagging in context** (`ctx2.py`, `ctx_tat.py`). spaCy reads each sentence whole; what
is kept is the triple *form + POS + whether it opened the sentence*. A sentence-initial
capital is orthography, not lexicon: spaCy reads a verb there as a noun (`Denke ich…` gave
a phantom noun `Denke` at rank 569, `Mach das!` a Mach number at 255/M), so the fold has
to know which occurrences carry no case evidence.

**3. Folding to the lemma** (`foldlib.py`). Spelling normalisation (`laßt` → lasst,
`fleissig` → fleißig) → the lemma of its own POS → a POS-strict form edge → **a hop through
simplemma** (`bessere`: no adj edge, but the dictionary form `besser` has one — `gut`) →
a POS-loose fallback. X tokens (English inside subtitles) are dropped: 413 occurrences of
`my` were being credited to the Greek letter `My`. Sentence-initial NOUN/PROPN occurrences
whose lowercase twin conjugates a verb are dropped (142,076 tokens) — the Denke/Schalte/
Mach phantoms — and `Macht`'s rank moves honestly from 217 to ~100/M. The old 3× "dominant
merge" is gone: it silently ate `führen` → fahren, `hinweisen` → Hinweis, `bescheiden` →
beschissen.

**4. Filters** (`fold7.py` + `polish2.py`). Everything cut lands in `de-spoken-cut.tsv`
with its reason. Migaku subtraction expands word-form rows **by the row's part of
speech**: `führt`/`führte` (untagged) cover führen, an adjective row `verrückt` covers the
verb verrücken through its participle, but a noun row `frühstück` no longer covers the
verb frühstücken through the homographic imperative. Two edge-kind rules: a `degree` edge
into Migaku cuts a bare comparative (`besser`, `später`, `lieber`, `weiter` — each has a
"real" entry, and only the Komparativ page reveals it is inflection); a `both` edge (a
word that is at once a marginal dictionary lemma and a declined form of a Migaku word) cuts
`Gedanken`/`Namen` — but only without en confirmation (`Falle`, `Mais`, `Alte` survive) and
only at ~100 % of its own mass (`Tote` is fed by `Toten`, `gefallen` by `gefällt`/`gefiel`
— both survive). `polish2` collapses the declensions of nominalised adjectives (the licence
is the adjective **or one of its forms**: `beste` → gut groups Beste/Bestes/Bester, whose
stem `best` is no headword), ß/ss doublets inside the list, nominalised infinitives, and
junk rows.

**5. Blending** (`blend.py`). Rank = weighted geometric mean: film^0.7 × everyday^0.3.
The weight is **measured** (2026-08-24, α ∈ {0.5…1.0}): 0.7 is where the skews balance
in the top 400 (5 % of words >4× skewed towards film against 4 % towards Tatoeba; at 0.5 it
was 2 %/8 % — textbook vocabulary on top, `regnen` #8; at pure film 13 %/0 % — weapons on
top). At 0.7 `regnen` moves 8 → 22, `leihen` 9 → 16, and the crime skew stays suppressed:
`Knarre` 1045 (film alone would say 245), `Hurensohn` 723 (118), `Anklage` 327 (112).

**6. The personal pass** (`known.py`, 2026-08-24/25, at the user's request: forms of words
already known, and "obvious" words like *Kilo*). Four classes, each a rule plus a
hand-reviewed keep-list: **nominalisations** of Migaku adjectives (`Beste` ← gut, `Neues`,
`Tote`, `Junge` ← jung; kept: lexicalised homographs `Gewissen`, `Taube`, `Sitte`,
`Freier`, `Wilderer`), **declensions/degrees** (`weiter`, `lange`, `blöde` — a deliberate
reversal of the audit's "lange/weiter recovered" call), **verb forms** (`gefallen` ←
fallen, `geraten`, and preterite ghosts `riefen`, `schliefen`; kept: real verbs with an
accidental Konjunktiv edge — `betrügen`, `gelangen`, `rasten`, `tränken`), and
**transparent internationalisms** (984 words: candidates from an English-cognate match
over wordfreq, the verdict by word-by-word review; a keep-list of ~110 false friends —
`Teller` ≠ teller, `List` ≠ list, `Taste` = key, `After` = anus, `Provision` = commission,
`Pups`). A fifth class (2026-08-25): **derivatives of geography** — koreanisch / Koreaner /
Koreanerin / Koreanisch and the whole family of nationalities, by an explicit
geographic whitelist (matching on corpus names gave 80 false hits out of 237: `Bäcker` ←
Back, `Beere` ← Beer; kept: `Galle` ≠ Gallien, `Bremse` ≠ Bremen, `Haifisch` ≠ Haifa,
`Sparte` ≠ Sparta). Plus contractions of known adverbs (`drüber`, `drum`, `rauf`),
`Eltern` (Migaku lemma `elter`), a hand list of the obvious (`Franzose`, `Kretin`,
`Knute`), and seven names that slipped through as dictionary homonyms (`Tara`, `Parker`).

**7. Glosses** (`ru.py` + `ru_fill.py`, 2026-08-25). The Russian column: the German
Wiktionary's own Übersetzungen (exact translations, `Junge → мальчик`) → the Russian
Wiktionary's definitions of German headwords (`Koffer → чемодан; сундук; кофр`, grammar
entries of the "form of …" kind filtered out) → the uncovered rest (colloquial prefixed
verbs, compounds) in batches through `claude -p` on the project's echoed-id protocol. The
dictionaries cover ~78 %.

## Tried and rejected

More important than the rules: each of these would have deleted good words.

- **"Has a page ⇒ is a lemma"** — the first build. German Wiktionary gives real pages to
  conjugated forms (`brachte`), declined forms (`bessere`) and pre-reform spellings
  (`laßt`, `wüßte`): the lemma table certified them as vocabulary, and the 3× merge bolted
  on to compensate swallowed real words through junk edges.
- **One "real" sense ⇒ lemma** — form pages pick up stray real senses: `Freunde`
  ("friends" in en), `Tage` ("that time of the month"), `gedacht` (a glossless sense in
  de). The page-level tag and the "… form" template outvote them; `Tage` sat at #9 of the
  rebuild before this rule.
- **`alt_of` as a spelling edge always** — en marks `Gesetz` "short for Gesetzessammlung",
  `Weizen` "short for Weizenbier", `Kugel` "variant of Kobel". Believing it, the pipeline
  deleted Gesetz/Weizen/Kugel from the lemmas, and 554 occurrences of `Kugel` (bullets!)
  went to a squirrel's nest, while `Gesetzsammlung` stood at 73/M. A spelling edge applies
  only to a word with no real senses.
- **Subtracting comparatives by frequency or POS logic** — cannot tell `führen`/`fahren`
  (Konjunktiv II) from `Tage`/`Tag`. What can: the `degree` marker, the share of a word's
  own mass (a verb is spread over its conjugation — a noun or a comparative is not), and en
  confirmation of lemma status.
- **Expanding Migaku rows without part of speech** — the noun row `frühstück` (breakfast)
  covered the verb frühstücken through the homographic imperative; simplemma did the same
  (`bescheid` → bescheiden). By the row's POS instead: nn → noun (+adj for nominalisations:
  `erster`), adj → adj/adv/**verb** (`verrückt` is the participle of verrücken), v → verb.
- **"Absent from written German"**, **reduction over `forms`** (`Junge` → jung —
  derivation!), **"has its own entry ⇒ lemma"**, **lowercase only**, **repeated letters**,
  **PROPN alone** — all from the first build; the reasons are in the scripts' docstrings.

**Nominalised adjectives stay** (`das Beste`, `der Fremde`, `die Süße`); only their
declensions are collapsed. **The `lange`/`weiter` class:** an adverb with its own
dictionary entry stays (`lange` #3), a bare comparative leaves with a reason
(`besser`/`später`/`lieber`/`weiter` — "comparative of X, in Migaku"); the boundary is
whether the word has a Komparativ page. Debatable on exactly these words; one rule, and
flipping it is one line.

## Audits

**2026-08-24 (what was fixed):**

1. Pre-reform word forms (`laßt` #267, `gewußt`, `wüßte`, `Anlaß`…) — 0 in the new list.
2. Internal ß/ss doublets (15 pairs: `fleißig` + `fleissig`…) — 0.
3. Declension doublets (`Beste` #6 + `Bestes` #58 + `Bester`) — one word each.
4. Imperative phantoms (`Denke` #569, `Schalte`, `bessern` #14 from besser forms) — gone or
   at honest ranks (`bessern` #1131).
5. Silently lost words (`führen` → fahren, `bescheiden` → beschissen, `hinweisen`,
   `frühstücken`, `rechts`, `Schätzchen`) — restored (`rechts` #19, `bescheiden` #171,
   `frühstücken` #197) or subtracted honestly with a record (`führen` — Migaku has
   `führst/führt/führte`; `namens` — a preposition, like every function word). Losses from
   SUBTLEX's top 3,000 outside its own lemmatisation artefacts: 0.
6. Tatoeba's textbook tilt (`regnen` #3) — weight 0.7, see *Blending*.

**2026-08-26 (the resolver, two classes)**, found by checking `film_pm` against SUBTLEX (a
word ≥4× more frequent in our corpus is a suspect; the check is a per-component breakdown
of `fold_corpus`, which forms feed the lemma):

1. **The spelling-edge teleport.** en-wikt calls `Weißen` an obsolete spelling of `Weizen`,
   and `normalize()` followed spelling edges before anything else — every *die Weißen* flew
   into wheat (297 of 339 tokens, #100 for a 3/M word). Same rule as for `alt_of`: a spelling
   edge is trusted only on a form with no other reading (no form-of edges, no lemma of its
   own). `laßt`, `Weitzen`, `fleissig` have no reading and kept their edges. `Weizen` →
   #1677.
2. **ADV-tagged predicatives fell into rare verbs.** spaCy tags a predicative adjective as
   ADV (a fact `fold7`'s filter already knew — `lange`, `weiter`) and the resolver did not:
   the strict ladder with want=adv found no adj lemma, and the any-POS fallback gave the
   word to a verb through a homographic inflection edge. `hervorragend` fed hervorragen
   (16/M for a verb honestly at 0.15/M); bare `munter`/`steif`/`schräg` fed
   muntern/steifen/schrägen through imperatives. ADV now also stands on the adj entry (adv
   first, so real adverbs win). Participial adjectives got their mass back and entered the
   head: `interessiert` #3, `entfernt` #8, `besorgt` #17, `hervorragend` #80;
   hervorragen/steifen/schrägen went to "too rare".

Accepted leftovers: `auszeichnen` #96 — *Ausgezeichnet!* with a VERB tag honestly folds into
the verb (folding participles is the ratified policy; the adjective stands beside it);
`Schuppen` — a homograph (scales/shed) the NOUN tag does not split; `klinken` #3969 — a
Colonel Klink (PROPN) through the bare simplemma fallback, outside the top 3k.

## Checks

- Intersection of the list with Migaku (exact, lowercased) — **0 words**.
- SUBTLEX's top 3,000 lemmas: each is either in the list, in `de-spoken-cut.tsv` with a
  reason, or in Migaku; no unexplained losses (the remainder is SUBTLEX's own
  lemmatisation artefacts: `besonder`, `diejenig`, `worden`).
- Order was compared with SUBTLEX-DE on the first build (Spearman 0.86, 91 % overlap); the
  ranking mechanics have not changed since — the dictionary under them has.

## Limitations

- **The tail is thin.** 1,013 lemmas have 100+ occurrences; 51 % of the list stands on
  fewer than 10. Up to rank ~2,500 the order can be trusted; beyond that it is "a word from
  roughly this band". The working part is the first 3–5 thousand.
- **"Not in Migaku" ≠ "unknown".** The head of the list is words without a Migaku row
  (`Junge`, `Macht`, `wachsen`, `Weihnachten`): Migaku simply never met them in tagged
  content. All of Migaku is subtracted, any status — that was the instruction.
- **Tatoeba is artificial** — sentences for learners; slang is under-represented. The 0.3
  weight allows for that.
- Known burrs: `Einkunft` #6602 (only `Einkünfte` is real; the en entry holds the lemma),
  `Taliban` #5701, `der Gefallen` lost (the "nominalised infinitive" rule gave it to the
  verb gefallen #4).

## The subtitle list (`de-yt.tsv`) and how it relates to the frequency list

A second list on a different principle: not "what is frequent in spoken German in general"
but **what is said on the channels the user actually watches** (406 channels, 12,249
words; columns `rank, word, ru, pm, channels, cum, migaku`). Checked against the frequency
list and against the `Frequency 🇩🇪` deck, 2026-08-27:

**Overlap.** 877 of the deck's 1,000 words occur in the subtitles — same language. But they
sit far down: the median subtitle rank of a deck word is **#2670**, 308 of them make the
subtitle top 2,000, 60 fall past #8000, and the whole deck covers **8.4 %** of the list's
word occurrences. **123 deck words are never said on 406 channels** — `Sarg`, `Pfarrer`,
`Verdächtige`, `Geschworene`, `Handschelle`, `Kaution`, `Flitterwochen`, `Truthahn`,
`Köder`, `Hurensohn`: the film skew that FILM_W=0.7 dampened but did not remove.

**The head of the subtitle list is bookkeeping, not gaps in knowledge.** Of its top 500,
453 are absent from `de-words.tsv`, and **435 of those were cut there as "already in
Migaku"**, while the subtitle list marks 438 of its own top 500 as `ungraded`. Both
pipelines read the same Migaku and diverge: the frequency list counts any Migaku row as
known, the subtitle list keeps ungraded rows and puts them first — hence `eigentlich,
deswegen, eben, ob, sondern` at its head. Genuinely new words in the top 500: **6**, and
they are English noise (`me`, `Content`, `Piece`, `Are`, `gaming`) plus `afd`. **The user's
decision (2026-08-27): `ungraded` counts as known** — 2,409 rows; otherwise the first cards
would teach `ob`.

**What the subtitle list sees and the frequency list does not.** 728 words that
`de-words.tsv` pushes past #1000, the subtitles put in the top 3,000, and Migaku does not
know:

| word | subtitles | de-words |
|---|---|---|
| `teilweise` | #70 | #1338 |
| `generell` | #97 | #3669 |
| `abonnieren` | #135 | #6133 |
| `dementsprechend` | #185 | #9365 |
| `beispielsweise` | #193 | #5081 |
| `verlinken` | #330 | #14886 |
| `Endeffekt` | #460 | #9105 |

Two classes: **the connectives of a spoken monologue** (teilweise, generell,
dementsprechend, währenddessen, zwischendurch, nachvollziehen, spätestens) and
**YouTube-native vocabulary** (abonnieren, hochladen, verlinken, Endeffekt, Blende, Abo).
The film + Tatoeba blend under-rates them systematically — not a defect, a different corpus.

**What the list lacks is cleanliness.** Its top 3,000 holds 251 words absent from
`de-words.tsv` altogether, mostly English intrusions (`me`, `My`, `random`, `easy`,
`Content`, `beach`, `Green`), YouTube jargon, lowercased proper nouns (`picasso`,
`ägypten`), abbreviations (`ai`, `afd`) and forms (`ums`, `übersetzt`). **The German lemma
table does not work as a filter** — measured: `me`, `My`, `random`, `easy`, `Content`,
`Green` all have entries in de.wiktionary. So the selection is made by an LLM, in one pass
together with the gloss (`freqdeck/wordlist.py`), and every rejection with its reason lives
in `freqdeck/out/yt-verdicts.tsv` — the filter is checked by what it removed, not on trust
(the lesson of `known.py`).

**A burr in the frequency pipeline found by this comparison:** `Nagel` was cut by the "NE
in 77 % of DeReKo" rule as a proper noun. It is a real word; worth re-checking how many more
went that way.

## The `Frequency 🇩🇪` deck

The pipeline "first N words of this list → Anki cards with generated sentences" lives in
[`freqdeck/`](../freqdeck/README.md), with its own README and tests. From here it takes only
`de-words.tsv` (and `de-yt-deck.tsv` for the second list).

## Rebuilding

```sh
cd freq
gunzip -k *.tsv.gz                     # the intermediate tables, if you have them
uv run --with simplemma --with pyobjc-framework-quartz python fold7.py
uv run --with simplemma --with pyobjc-framework-quartz python polish2.py
uv run --with simplemma python blend.py
uv run --with simplemma --with wordfreq python known.py
uv run python ru.py                    # → de-ru.tsv + de-words.tsv (needs the dumps)
```

The dumps (`dewikt-raw.jsonl.gz`, `ruwikt-raw.jsonl.gz`) were deleted after the first pass;
without them the glosses come from the caches, as on 2026-08-26: the new word order is
joined to `de-ru.tsv` (last pass's word → gloss) + `de-ru-fill.tsv`, the gaps are filled by
`uv run python ru_fill.py`, and the first 3,000 rows go to `de-words-3k.tsv`.

From scratch (~2 GB of downloads and two spaCy passes, ~40 minutes):

```sh
curl -Lo dewikt-raw.jsonl.gz \
  https://kaikki.org/dewiktionary/raw-wiktextract-data.jsonl.gz
curl -Lo enwikt-german.jsonl.gz \
  https://kaikki.org/dictionary/German/kaikki.org-dictionary-German.jsonl.gz
curl -Lo os_de_sentences.txt.gz \
  https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/mono/de.txt.gz
curl -LO https://downloads.tatoeba.org/exports/per_language/deu/deu_sentences.tsv.bz2
bzcat deu_sentences.tsv.bz2 | cut -f3 > tat_sentences.txt
python3 wikt.py                        # → de_lemmas/de_formof/de_forms.tsv
SPACY=~/Library/"Application Support"/Anki2/addons21/spacy-venv-python-3_13/bin/python
"$SPACY" ctx_tat.py && "$SPACY" ctx2.py    # → tat_forms.tsv, os_forms.tsv (~40 min)
# then fold7 → polish2 → blend as above
```

The spaCy interpreter is the one AnkiMorphs installs beside Anki (`de_core_news_md`); the
same venv is what `lib/vocab/words.py` uses for German lemmas.
