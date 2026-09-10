"""Rank on two independent LIVING registers instead of film dialogue alone.

Ranking on subtitles alone put `Gewehr`, `Anklage`, `Knarre`, `Hurensohn` near the top:
measured, the median word in the top 400 is 2.6x commoner in film than in everyday German,
because film over-represents crime, weapons and courtroom talk.

The correction needs a second source that is alive but not cinema. What was checked:

  * **Leipzig `web-public` and `mixed-typical`** — sampled, and NOT informal: press
    releases, medicine leaflets, bank marketing (`Sparkassen-Gesundheits-Schutz Plus`,
    `Einnahme mit anderen Arzneimitteln`). Institutional written German. Blending it in
    would drag the list toward exactly the register we are escaping, so it rides along as
    a column and takes no part in the ranking.
  * **GeRedE, 270M tokens of German Reddit** — the ideal source, and not downloadable:
    CQPweb access only, GitHub carries the scripts and not the text.
  * **Tatoeba German**, 777,664 sentences of everyday utterances — `Du musst kommen`,
    `Erinnerst du dich an dieses Spiel?`. Constructed rather than naturally occurring, and
    textbook-flavoured, so it under-represents slang; but it carries no crime skew, which
    is precisely the bias being corrected.

Both living sources go through the identical pipeline — ONE fold, foldlib.py, shared with
fold7 (it used to be a copy, with a comment promising the copies matched). The rank is a
WEIGHTED geometric mean, film raised to FILM_W: the plain mean (0.5/0.5) gave 6M tokens
of constructed learner sentences an equal vote against 13.7M of dialogue, and the head
showed it — `regnen` at #3 and `leihen` at #4 on a 7–10x Tatoeba skew, the vocabulary of
`Es regnet. Kannst du mir dein Buch leihen?`. FILM_W = 0.7 is measured (2026-08-24, on
the rebuilt fold, α ∈ {0.5…1.0}): it is where the top-400 skew balances — 5% of words
>4x film-heavy against 4% >4x Tatoeba-heavy (0.5 gives 2%/8%, film alone 13%/0%) — the
textbook head clears (`regnen` 8 → 22, `leihen` 9 → 16, `Lärm` 58 → 89) and the crime
demotion the blend exists for survives (`Knarre` 1003 against 245 on film alone,
`Hurensohn` 691 against 118, `Anklage` 309 against 112). A floor keeps absence from one
source from zeroing a word outright: `Knast` is real German that Tatoeba never had
occasion to use (it lands at 98 — film alone says 29, the plain mean said 238).
"""
import gzip
import json
import math
import os
import sys

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(D))
sys.path.insert(0, D)
import foldlib

FLOOR = 0.15          # per million: what "this source never used the word" is worth
FILM_W = 0.7          # film's exponent in the geometric mean; measured, see above

dic = foldlib.Dict(D)
film_f, _, _, ftok, _, fdrop, _ = foldlib.fold_corpus(f"{D}/os_forms.tsv", dic)
tat_f, _, _, ttok, _, tdrop, _ = foldlib.fold_corpus(f"{D}/tat_forms.tsv", dic)
film = {w: n * 1e6 / ftok for w, n in film_f.items()}
tat = {w: n * 1e6 / ttok for w, n in tat_f.items()}
print(f"film   {ftok:,} tokens · {len(film):,} lemmas ({fdrop:,} initial-NOUN dropped)",
      file=sys.stderr)
print(f"tatoeba{ttok:>8,} tokens · {len(tat):,} lemmas ({tdrop:,} initial-NOUN dropped)",
      file=sys.stderr)

# the institutional web list, for the column only — built by the older pipeline, so it is
# matched case-insensitively and never used to rank
web = {}
with gzip.open(f"{D}/lists-normalised.json.gz", "rt", encoding="utf-8") as f:
    for w, v in json.load(f)["WEB"].items():
        web[w.lower()] = max(web.get(w.lower(), 0), v)

rows = [l.rstrip("\n").split("\t") for l in open(f"{D}/de-spoken-final.tsv", encoding="utf-8")][1:]
out = []
for r in rows:
    w = r[1]
    a, b = film.get(w, 0.0), tat.get(w, 0.0)
    live = max(a, FLOOR) ** FILM_W * max(b, FLOOR) ** (1 - FILM_W)
    skew = math.log2(max(a, FLOOR) / max(b, FLOOR))
    out.append((w, live, a, b, web.get(w.lower(), 0.0), skew, r[3], r[4]))
out.sort(key=lambda t: -t[1])

with open(f"{D}/de-living-german.tsv", "w", encoding="utf-8") as f:
    f.write("rank\tlemma\tliving_score\tfilm_pm\teveryday_pm\tweb_pm\tfilm_skew\tpos\twiktionary_pos\n")
    for i, (w, live, a, b, wb, sk, pos, wp) in enumerate(out, 1):
        f.write(f"{i}\t{w}\t{live:.2f}\t{a:.2f}\t{b:.2f}\t{wb:.2f}\t{sk:+.2f}\t{pos}\t{wp}\n")

old = {r[1]: i + 1 for i, r in enumerate(rows)}
print(f"\nwritten {len(out):,} rows\n", file=sys.stderr)
print("Top 40 on the blended ranking (film rank in brackets):", file=sys.stderr)
for i in range(0, 40, 4):
    print("   " + "".join(f"{w:<16}[{old[w]:>5}]  " for w, *_ in out[i:i + 4]), file=sys.stderr)
drop = sorted(((w, old[w], i + 1) for i, (w, *_) in enumerate(out) if old[w] < 400),
              key=lambda t: t[1] - t[2])[:16]
print("\nBiggest fallers — what the film skew had inflated:", file=sys.stderr)
print("   " + ", ".join(f"{w} {o}→{n}" for w, o, n in drop), file=sys.stderr)
