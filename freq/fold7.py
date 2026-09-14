"""Final build: corpus forms → candidate lemmas → the filter, every cut with a reason.

The fold itself (form + in-context POS → lemma) lives in foldlib.py, shared with
blend.py — it used to be duplicated here and the 2026-08-24 audit is why it moved. What
also went in that audit: the 3x dominant-lemma merge that used to sit after resolution.
It existed to mop up conjugated forms that the polluted lemma table certified as words
(`brachte`, `Träume`), and its cure was worse than the disease — it followed any
form_of edge regardless of POS or quality, so `führen` (a top-250 verb) went into
`fahren` through an archaic sense, `hinweisen` into `Hinweis` through a capitalised
dative plural, and none of it reached the cut file: the words simply vanished. With
wikt.py classifying senses, a form page never becomes a candidate and a true lemma is
never attributed elsewhere, so the merge has nothing left to do and every word that
enters this file leaves it as either a kept row or a cut row with a reason.

The filter rules are unchanged in spirit; the one lesson each carries is written beside
it. PROPN alone is not enough (that cost `hey`, `hallo`, `Oma` before corroboration was
required); closed-class words are a fixed list already in front of him in every
sentence; and everything Migaku knows — at any status, that was the instruction — leaves.
"""
import os
import sys
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(D))
sys.path.insert(0, D)
import simplemma
import foldlib

PROPN_MAX, NE_MAX, MIN_N = 0.5, 0.6, 4
# Closed-class words are a fixed list of roughly 200 items, not vocabulary to acquire —
# and every one of them is already in front of him in every sentence he watches. They
# survive the lemma pipeline because `das`, `denen`, `dessen` are legitimate headwords.
FUNCTION_UPOS = {"DET", "PRON", "ADP", "CCONJ", "SCONJ", "PART", "AUX"}
REJECT_POS = {"name", "character", "punct", "symbol", "prefix", "suffix",
              "interfix", "circumfix", "romanization", "abbrev", "phrase", "proverb"}

dic = foldlib.Dict(D)
print(f"{len(dic.head):,} lemma keys · {len(dic.fof_any):,} inflected forms · "
      f"{len(dic.spell):,} spelling variants", file=sys.stderr)

freq, pos_of, propn, total, sfreq, dropped, own = foldlib.fold_corpus(f"{D}/os_forms.tsv", dic)
print(f"corpus {total:,} tokens → {len(freq):,} lemmas "
      f"({dropped:,} sentence-initial NOUN sightings of verb twins dropped)", file=sys.stderr)

# ------------------------------------------------------------------ evidence
tot, ne = defaultdict(float), defaultdict(float)
for line in open(f"{D}/DeReKo-2014-II-MainArchive-STT.100000.freq",
                 encoding="utf-8", errors="replace"):
    p = line.rstrip("\n").split("\t")
    if len(p) != 4:
        continue
    try:
        v = float(p[3])
    except ValueError:
        continue
    tot[p[0].strip().lower()] += v
    if p[2].strip() == "NE":
        ne[p[0].strip().lower()] += v
ne_share = {w: ne[w] / tot[w] for w in tot if tot[w] > 0}

from lib.vocab import migaku
rows, _ = migaku.dump_rows()
de = [r for r in rows if str(r[3]).lower().startswith("de")]
# A Migaku row names its part of speech, and the form-edge expansion must respect it:
# Migaku's `führt`/`führte` (untagged) really are the verb führen — that expansion is
# what lets those rows subtract it — but `frühstück` and `bescheid` are NOUN rows
# (Frühstück, Bescheid) whose lowercase spelling doubles as a verb imperative, and an
# unrestricted expansion read them as frühstücken and bescheiden and cut both verbs.
# An ADJ row does follow verb edges — `verrückt`/`betrunken` ARE participles, and
# without that hop their verb-tagged corpus mass minted `verrücken` at rank 7 — and a
# noun row follows adj edges, because Migaku files nominalisations as nouns (`erster`).
MIG_POS = {"nn": {"noun", "adj"}, "nnp": {"noun", "name"}, "adj": {"adj", "adv", "verb"},
           "adv": {"adv", "adj"}, "v": {"verb"}}
# …and the simplemma hop obeys a STRICTER gate than the edges: simplemma answers with
# no POS, so its fold from a tagged row counts only into the row's own class — the noun
# row `bescheid` must not cover the adjective bescheiden just because nouns were
# allowed to cover nominalisations (`erster`→erste goes through the edges, not here).
MIG_STRICT = {"nn": {"noun"}, "nnp": {"noun", "name"}, "adj": {"adj", "adv"},
              "adv": {"adv", "adj"}, "v": {"verb"}}
MIGAKU = set()
for r in de:
    d = str(r[0])
    MIGAKU.add(d.lower())
    ptag = str(r[2] or "").lower()
    want = MIG_POS.get(ptag)
    m = simplemma.lemmatize(d, lang="de").lower()
    if want is None or dic.head.get(m, set()) & MIG_STRICT[ptag]:
        MIGAKU.add(m)
    for f in (d, d.capitalize()):
        for p, ts in dic.fof[f].items():
            if want is None or p in want or p == "x":
                MIGAKU |= {t.lower() for t in ts}
        t = dic.spell.get(f)
        if t:
            MIGAKU.add(t.lower())
print(f"Migaku German: {len(de):,} rows → {len(MIGAKU):,} keys", file=sys.stderr)

# ------------------------------------------------------------------ filter
kept, cut = [], []
for w, n in sorted(freq.items(), key=lambda kv: -kv[1]):
    lo = w.lower()
    ps = dic.head.get(lo, set())
    top = max(pos_of[w], key=pos_of[w].get)
    # which lemma-POS would justify this word's tokens: spaCy tags predicative
    # adjectives ADV, so an ADV-dominant word counts as standing on an adj entry too —
    # `lange` and `weiter` are lemmas of the position they were counted in, and the two
    # form-cut rules below must never treat such a word as somebody's inflection.
    lemma_of = {foldlib.UPOS2WIKT.get(top)} | ({"adj"} if top == "ADV" else set())
    why = None
    if n < MIN_N:
        why = "too rare"
    elif not ps:
        why = "not a German Wiktionary headword"
    elif propn[w] / n > PROPN_MAX and ("name" in ps or ne_share.get(lo, 0) > NE_MAX):
        # PROPN alone is not enough, and finding that out cost the best words in the list.
        # spaCy tags a greeting and a term of address as a proper noun — `hey` (1,139 per
        # million), `hallo`, `hi`, `tschüss`, `OK`, `Oma`, `Mami`, `Schluss` were all
        # thrown out as names. So the sentence's judgement now needs corroboration: the
        # dictionary must carry a `name` sense, or DeReKo must tag it NE. `Sam`, `Michael`,
        # `Paris` have one; a greeting has neither.
        why = f"proper name (PROPN {propn[w]/n:.0%} in context, and a name in the dictionary)"
    elif not ps - REJECT_POS:
        why = f"proper name / non-word ({','.join(sorted(ps))})"
    elif ne_share.get(lo, 0) > NE_MAX:
        why = f"tagged NE in {ne_share[lo]:.0%} of DeReKo"
    elif lo in MIGAKU:
        why = "already in Migaku"
    elif top in FUNCTION_UPOS:
        why = f"closed-class function word ({top})"
    elif (top in ("ADJ", "ADV") and own.get(w, 0) >= 0.9 * n
          and {t.lower() for t in dic.degree.get(w, set()) | dic.degree.get(lo, set())}
          & MIGAKU):
        # A bare comparative/superlative of a Migaku word. Both dictionaries grant
        # `besser`, `später`, `weiter` real adverb entries of their own, which is what
        # kept them out of every other rule — but a Komparativ page is the marker that
        # the word IS the base word's inflection, and the user's standing decision is
        # that inflection folds while derivation stands. `lange` has no degree page
        # (it is lang's ADVERB, not its comparative) and stays. The own-share guard
        # keeps this away from verbs: `gefallen` draws most of its mass through
        # gefällt/gefiel and no degree rule may touch it.
        t0 = sorted({t for t in dic.degree.get(w, set()) | dic.degree.get(lo, set())
                     if t.lower() in MIGAKU})[0]
        why = f"comparative of {t0}, in Migaku"
    elif (top == "NOUN" and own.get(w, 0) >= 0.9 * n
          and (lo, "noun") not in dic.enreal
          and {t.lower() for t in dic.both[w].get("noun", set())
               | dic.both[lo].get("noun", set())} & MIGAKU):
        # A noun that is simultaneously a marginal dictionary lemma and the declined
        # form of a Migaku word, with ~all of its corpus mass arriving as its own
        # spelling: dewikt files `Gedanken` as a variant lemma, and 1,115 of its 1,119
        # sightings are simply the plural of `Gedanke`, which Migaku already has. Real
        # words with the same shape are safe three ways: an en-confirmed noun homograph
        # (`Falle`, `Mais`, `Alte`) is exempt outright, `Tote` draws a third of its
        # mass from `Toten` (own-share fails), and `Beste`/`Junge` point at
        # Bester/Junges, which are not in Migaku.
        t0 = sorted({t for t in dic.both[w].get("noun", set())
                     | dic.both[lo].get("noun", set()) if t.lower() in MIGAKU})[0]
        why = f"declined form of {t0}, in Migaku (dictionary lemma page notwithstanding)"
    elif (not lemma_of & ps
          and {t.lower() for t in dic.fof_any.get(w, ())} & MIGAKU):
        # Only for a word standing here as a FORM — its dominant POS is not one it is a
        # lemma of. A word that earned its tokens as a lemma of that POS is never cut as
        # somebody's inflection: `lange` is an adverb lemma and also a declined form of
        # `lang`, and cutting it for the latter is how it went missing from the shipped
        # list. fof_any carries real inflection edges only — a `minor` edge (an archaic
        # or euphemistic cross-reference on a true lemma's own page, führen→fahren)
        # does not put a word here.
        why = ("inflection of "
               + sorted({t for t in dic.fof_any[w] if t.lower() in MIGAKU})[0] + ", in Migaku")
    elif (lo.replace("ß", "ss") in MIGAKU or lo.replace("ss", "ß") in MIGAKU
          or lo.replace("ae", "ä") in MIGAKU):
        why = "old/variant spelling of a Migaku word"
    elif (not lemma_of & ps
          and ({t.lower() for t in dic.wide.get(w, set()) | dic.wide.get(w.capitalize(), set())}
               | {simplemma.lemmatize(w, lang="de").lower()}) - {lo} & MIGAKU):
        why = "form of a Migaku word"
    (cut if why else kept).append((w, n * 1e6 / total, top, ps, why))

with open(f"{D}/de-spoken-final.tsv", "w", encoding="utf-8") as f:
    f.write("rank\tlemma\tper_million\tpos\twiktionary_pos\n")
    for i, (w, pm, top, ps, _) in enumerate(kept, 1):
        f.write(f"{i}\t{w}\t{pm:.2f}\t{top}\t{','.join(sorted(ps - REJECT_POS))}\n")
with open(f"{D}/de-spoken-cut.tsv", "w", encoding="utf-8") as f:
    f.write("lemma\tper_million\treason\n")
    for w, pm, _, _, why in cut:
        f.write(f"{w}\t{pm:.2f}\t{why}\n")

reasons = defaultdict(int)
for *_, why in cut:
    reasons[why.split(" (")[0].split(" in ")[0] if "NE" in why else why.split(" (")[0]] += 1
print(f"\nkept {len(kept):,} · cut {len(cut):,}", file=sys.stderr)
for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
    print(f"   {v:>7,}  {k}", file=sys.stderr)
print("\nFirst 100:", file=sys.stderr)
for i in range(0, 100, 4):
    print("   " + "".join(f"{w:<18}{pm:>7.1f}/M   " for w, pm, *_ in kept[i:i + 4]),
          file=sys.stderr)
