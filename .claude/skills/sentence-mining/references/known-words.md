# Known words — Migaku, and nothing else

The user's rule, stated directly: **only the words marked in Migaku count as known**. So
"known" here is not an inference, not an interval, not a frequency floor. It is one column in
one file.

This is the largest single difference from the Japanese skill this was converted from, which
computed "known" from Anki card intervals (a lemma is known once its highest card interval
reaches 21 days — AnkiMorphs' idea, recomputed live). That definition is not available here
and would not be wanted: it answers "have they drilled this in Anki", and the question is
"have they marked this in Migaku".

## The source

```
$BACKUP_DIR/migaku/de.csv        # BACKUP_DIR defaults to ~/vocab-backup
```

Written daily by `gigaku backup` (stage → gate → swap; it alerts Telegram when it fails or
goes stale). Columns: `dictForm, secondary, partOfSpeech, language, knownStatus, hasCard,
tracked, del, created, mod`. The known set is every row with `knownStatus == "KNOWN"` and
`del == "0"`, lowercased — **9,456 distinct forms** on 2026-09-04.

`check.py` reports the file's age and `analyze.py` warns past `known_words.max_age_days`
(14). Staleness is reported, never enforced: a slightly old set only means a few
freshly-marked words are offered again, which triage and review catch, whereas failing hard
would block a whole run over a backup that did not fire last night.

For a live read instead of the snapshot: `lib.vocab.migaku.read_chrome()` returns Migaku's
own `WordList` straight out of Chrome's IndexedDB, unmerged. It needs Chrome and can raise
`UserError`, so it is not the default.

### Why not gigaku's word cache

`~/Library/Caches/gigaku/words.json` is the obvious-looking source and is the wrong one, for
two independent reasons:

1. **It cannot be filtered by source.** `Word` has four fields — word, language, stage, date
   — and none of them is provenance. `merge()` keys on `(word, language)` and keeps whichever
   record has the latest date, so LR, Migaku and AnkiMorphs collapse into one anonymous
   record. Measured: of its 9,798 German words, **345 are not Migaku-KNOWN** (328 carry the
   year-1 sentinel that marks an AnkiMorphs assertion Anki could not date; the rest are LR
   leftovers and words `keep` rescued). Those 345 are exactly what the user's rule excludes,
   and nothing in the file can point at them.
2. **`gigaku words --lang de --stage known` rewrites the cache as a side effect of being
   read** — it calls `import_words()`, which re-imports from Chrome and Anki and writes
   `words.json` back. A read that mutates its own source is not something a mining run should
   be doing.

## Matching a token against it

Migaku's `dictForm` is **not a lemma**. Real entries: `piratenköniginnen`, `brukterern`,
`anschaust`, `sitcoms`. spaCy's lemma of the same words is `Piratenköniginn`, `Brukterer`,
`anschauen`. Neither side can be normalised onto the other, so both directions are tried:

- the known set is built as **Migaku's forms ∪ the spaCy lemma of each form** — 9,456 → 10,114;
- a token counts as known if **its surface** or **its lemma** is in that set.

Warm-started from `~/Library/Caches/gigaku/de_lemmas.json`, the ~10k-entry map gigaku already
paid to compute, so a normal run lemmatises only the handful of forms the cache has never
seen (measured: 3 of 9,456).

### One tokenizer on both sides

The known set and the mined sentences go through the **same** `de_core_news_md` pipeline, for
the same reason the Japanese skill insisted on one SudachiPy: a lemma the user knows must be
spelled identically in both places, or known words leak through as false unknowns. The model
lives in AnkiMorphs' own spaCy venv —

```
~/Library/Application Support/Anki2/addons21/spacy-venv-python-3_13/bin/python
```

— which is also the model AnkiMorphs' recalc matches against, so this skill and the add-on
agree on what a German lemma is. `analyze.py` re-execs itself under that interpreter. It is
rebuilt against uv's cpython and **dies whenever Anki bumps python**; `check.py` is what tells
you so, before three and a half hours of audio have been downloaded.

## The gate: absent ≠ unknown

A word missing from Migaku is not thereby a word the user does not know — Migaku only records
what they happened to mark. Measured across one playlist (2,313 sentences): **469 unknown
lemmas sit in the top 1,500 of `freq/de-yt.tsv`**, headed by `eigentlich` (#1 in spoken
YouTube German), `bekommen` (#9), `sogar` (#13), `geil` (#14), `fahren` (#19).

Those are bookkeeping gaps. They are:

- **not carded** — a deck two-thirds full of A1 adverbs teaches nothing;
- **not silently assumed known** — that would be the frequency floor the user has twice
  rejected;
- **written to `grade-me.tsv`** with rank, per-million frequency, Russian gloss and the
  sentence they were heard in, to be marked in Migaku. Mark them, re-run, they disappear.

They *do* count as known while computing a sentence's i-level, because otherwise almost every
sentence is i+3 and the i+1 signal — the entire point of the method — is gone. The honesty is
preserved by the list, not by the arithmetic.

`frequency.grade_gate` (default 1500) is the knob; `analyze.py --gate N` overrides it for one
run. Higher sends more words to grading and makes fewer cards.
