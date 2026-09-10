# Note type — `🇩🇪 German`, and which of its 16 fields this writes

The deck must be indistinguishable from `Frequency 🇩🇪` in review. That is why this skill
writes only the four fields it can honestly fill from a video, and hands the card to
gigaku's own `freqdeck/definitions.py` to finish — the same code, prompts, model and voice
that made the 1,993 notes already in the collection.

## The fields

```
Sentence · am-study-morphs · Word · Word Audio · Definition · Notes · Sentence Audio ·
Definition Audio · am-all-morphs-count · My Alternatives · My Clarity · My Run · Image ·
Context · My Sort Key · am-all-morphs
```

Byte-identical to `🇯🇵 MvJ` by the user's standing rule — German **is** the Japanese setup
since 2026-08-13: one template, one tag convention, one importer.

| field | who writes it | what |
|---|---|---|
| `Word` | **this skill** | the spaCy lemma of the unknown word |
| `Sentence` | **this skill** | the sentence as YouTube transcribed it |
| `Sentence Audio` | **this skill** | `[audio:sm_de_pNN_nnn_slug.mp3]` — ffmpeg cut from the episode |
| `Context` | **this skill** | the episode URL seeked to the sentence |
| `Definition` | `freqdeck/definitions.py` | MvJ's own 💣 block, gpt-4o, `def-type="bilingual"` |
| `Definition Audio` | `freqdeck/definitions.py` | OpenAI `tts-1`/`shimmer` of that text |
| `Word Audio` | `freqdeck/definitions.py` | Wikimedia Commons `De-<Wort>.ogg`, TTS on a miss |
| `Image` | **nobody** | deliberately empty — see below |
| `Notes` | **`translate.py`** | the Russian translation of the sentence — rendered on the back, and what `freqdeck/audit.py` checks is Cyrillic |
| `am-*`, `My *` | AnkiMorphs / the add-ons | untouched |

## Three things that are easy to get wrong

**`[audio:…]`, never `[sound:…]`.** The 🇩🇪 front template wraps Sentence Audio in
`{{#Sentence Audio}}` and its JavaScript scans that div with `/\[audio:([^\]]+)\]/g` to build
the player. `[sound:]` exists only inside an `.apkg`, where MvJ's importer rewrites it on the
way in (`mvj/importer.py`). A note written with `[sound:]` through AnkiConnect is simply
silent. Every AnkiConnect-side writer in this repo — `definitions.py`, `fix_audit.py`, and
this skill's `push.py` — writes `[audio:]` directly.

(There is an older note in circulation saying 🇩🇪's front is a bare `{{Sentence Audio}}` and
that `[sound:]` is therefore load-bearing. That describes the 18-byte stub template from
before 2026-08-13 and is no longer true.)

**`Image` stays empty, with nothing in it.** Both templates wrap it in `{{#Image}}`, so an
empty Image renders nothing at all. The Japanese skill this was converted from wrote `。` into
an empty picture field, because *its* note type had a `{{^picture}}` branch on the back that
re-rendered the sentence audio and made it autoplay twice on mobile. This note type has no
such branch. Copying the filler across would put a stray `。` on every German card, which is
why `picture` is not in `field_map` at all rather than mapped-and-blanked.

**`Context` is the safe place for provenance.** It is the one field no template renders —
verified live against `modelTemplates`, and `mvj/german_template.py` actively refuses to sync
a Japanese template that starts rendering it. So the source link lives there and changes
nothing about how the card looks. `Notes` would have been wrong: it *is* rendered, on the back.

## Decks and tags

**One subdeck per channel: `YouTube 🇩🇪::<channel>`**, created on first push. The channel
comes from the manifest `fetch.py` writes, so a second channel lands beside this one rather
than mixing into it — and each gets its own new-card limit. A split into i+1 and a deferred sibling
bought only a second daily new-card limit — AnkiMorphs orders every 🇩🇪 new card on one global
morph-difficulty scale whatever deck it sits in.

**One tag written by this skill: `claude-sentence-mining`.** It is the provenance handle: it
survives a card being moved to another deck, and it is what a rollback matches on. Everything
else that used to be tagged is either clutter or somebody else's job:

| was tagged | why not any more |
|---|---|
| `<channel>`, `p01`…`p08` | the episode URL, seeked to the sentence, is already in `Context` |
| `i1`/`i2`/`i3` | it lied — only 72 of 159 cards tagged `i1` were really i+1. AnkiMorphs writes `_card-status::i+0` / `i+1` / `i+≥2` itself, and **that** is the tag to study by |
| `sm-audit-known` | an exact duplicate of `_card-status::i+0`, which AnkiMorphs keeps current |

`sm-audit-asr` / `-sense` / `-context` / `-missing` stay: they record **why** a card was
suspended, and nothing else does.

**Cards do get suspended** — by `audit.py --apply` (a mis-transcribed sentence, the wrong
sense, no content without the video) and by `recalc.py` (AnkiMorphs already knows the word).
Never deleted: a suspended card is one click from coming back, and every Anki-affecting step
here has to be individually revertible. Anki's review queue is still the final gate for
everything that survives.

Study query: `deck:"YouTube 🇩🇪" -is:suspended tag:_card-status::i+1`.

⛔ **AnkiMorphs recalc owns new-card order.** Whatever order these land in, the next **R**
rewrites the due numbers of every managed new card. Do not promise an order this skill cannot
keep.
