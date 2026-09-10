"""Normalise every downloaded German frequency list onto one lemma axis.

Wordform lists are lemmatised through the AnkiMorphs spaCy venv — the same model
gigaku's own `words._de_lemmas` uses — but with a *private* cache: writing into
`~/Library/Caches/gigaku/de_lemmas.json` would poison `_de_exported_forms()`,
whose keys-union-values is what stops the daily import re-inventing words.
"""
import csv
import json
import os
import re
import sys
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
TOP = 60_000            # forms per wordform list that reach the lemmatiser
CACHE = os.path.join(D, "lemma_cache_simplemma.json")

WORD_RE = re.compile(r"^[^\W\d_](?:[\w'’\-]*[^\W\d_])?$", re.UNICODE)


def ok(w):
    return 0 < len(w) <= 30 and WORD_RE.match(w) and not any(c.isdigit() for c in w)


# ---------------------------------------------------------------- loaders
def load_os2018():
    d = {}
    with open(f"{D}/os2018_full.txt", encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if len(p) == 2 and ok(p[0]):
                d[p[0]] = d.get(p[0], 0) + int(p[1])
    return d


def load_wordfreq():
    d = {}
    with open(f"{D}/wordfreq_de.txt", encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 2 and ok(p[0]):
                d[p[0]] = d.get(p[0], 0.0) + float(p[1])
    return d


def load_leipzig(name):
    d = {}
    with open(f"{D}/leipzig_{name}.words.txt", encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) == 3 and ok(p[1]):
                d[p[1]] = d.get(p[1], 0) + int(p[2])
    return d


def load_subtlex_forms():
    """SUBTLEX-DE word forms, case preserved."""
    d = {}
    with open(f"{D}/SUBTLEX_DE.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            w = (row.get("Word") or "").strip()
            try:
                n = int(row["WFfreqcount"])
            except (KeyError, TypeError, ValueError):
                continue
            if ok(w):
                d[w] = d.get(w, 0) + n
    return d


def load_subtlex_lemmas():
    """SUBTLEX-DE's own lemma column — no spaCy needed, and a check on ours."""
    d = {}
    with open(f"{D}/SUBTLEXDE_Lemmas.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            lem = (row.get("Lemma") or "").strip().lower()
            try:
                n = float(row["LemmaFreq"])
            except (KeyError, TypeError, ValueError):
                continue
            if ok(lem):
                d[lem] = max(d.get(lem, 0.0), n)   # LemmaFreq repeats per word form
    return d


def load_dereko():
    """token \t lemma \t POS \t freq over ~7bn tokens.

    The *token* column is what we keep: DeReKo's own lemmas are TreeTagger's, whose
    convention (`die` for the article, `eine` for the indefinite) is not spaCy's, and a
    list on its own axis cannot be rank-compared with the six that share ours.
    """
    d = defaultdict(float)
    with open(f"{D}/DeReKo-2014-II-MainArchive-STT.100000.freq",
              encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 4 or not ok(p[0].strip()):
                continue
            try:
                d[p[0].strip()] += float(p[3])
            except ValueError:
                pass
    return dict(d)


# ---------------------------------------------------------------- lemmatiser
# simplemma, not the AnkiMorphs spaCy venv. spaCy's German lemmatiser needs the tagger's
# POS, and a *single word with no sentence around it* is exactly where that guess fails:
# measured on this data it left `komm`, `musst`, `willst`, `gib`, `hab`, `dich`, `deinen`
# unlemmatised, which then read as words the user does not know. simplemma is a dictionary
# lookup, which is the right shape for a frequency list — there is no context to lose.
import simplemma


def lemmatise(forms):
    try:
        cache = json.load(open(CACHE, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    missing = sorted({f for f in forms if f not in cache})
    for i, w in enumerate(missing):
        try:
            lem = simplemma.lemmatize(w, lang="de")
        except Exception:
            lem = w
        cache[w] = lem if lem and any(c.isalpha() for c in lem) else w
        if i % 20000 == 0:
            print(f"  simplemma {i:,}/{len(missing):,}", file=sys.stderr, flush=True)
    if missing:
        json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return cache


# ---------------------------------------------------------------- build
def main():
    print("loading raw lists…", file=sys.stderr)
    raw = {
        "OS2018":  ("wordform", load_os2018()),
        "SUBTLEX": ("wordform", load_subtlex_forms()),
        "WORDFREQ": ("wordform", load_wordfreq()),
        "NEWS":    ("wordform", load_leipzig("deu_news_2024_1M")),
        "WEB":     ("wordform", load_leipzig("deu-de_web-public_2019_1M")),
        "WIKI":    ("wordform", load_leipzig("deu_wikipedia_2021_1M")),
        "DEREKO":  ("wordform", load_dereko()),
        "SUBTLEX_L": ("lemma", load_subtlex_lemmas()),
    }
    for k, (kind, d) in raw.items():
        print(f"  {k:10} {kind:8} types={len(d):>9,} tokens={sum(d.values()):>16,.0f}",
              file=sys.stderr)

    # top-N per wordform list
    tops = {k: dict(sorted(d.items(), key=lambda kv: -kv[1])[:TOP])
            for k, (kind, d) in raw.items() if kind == "wordform"}

    # One rule for every list: lowercase, then lemmatise. Restoring each lowercased
    # source's surface case from the case-preserving ones was tried and is worse —
    # sentence-initial "Mach" outvotes "mach" in subtitles, and simplemma then reads the
    # capital as a noun and returns it unchanged (`mach`, `guter`, `junges`, `weissen`
    # all leaked through as their own lemmas). Measured, lowercase costs almost nothing
    # on nouns (Haus/Kind/Mann/Stadt/Zeit all still fold) and fixes every one of those.
    # Uniformity is the load-bearing part regardless: the spoken↔written skew divides one
    # list's frequency by another's, so a word normalised two ways would fake a skew.
    surface = {}
    for k, d in tops.items():
        for w in d:
            surface[(k, w)] = w.lower()

    lem = lemmatise(set(surface.values()))

    lists = {}
    for k, d in tops.items():
        agg = defaultdict(float)
        for w, n in d.items():
            agg[lem.get(surface[(k, w)], surface[(k, w)]).lower()] += n
        lists[k] = dict(agg)
    for k, (kind, d) in raw.items():
        if kind == "lemma":
            lists[k] = d

    # relative frequency, per million
    out = {}
    for k, d in lists.items():
        tot = sum(d.values())
        out[k] = {w: n * 1e6 / tot for w, n in d.items()}

    json.dump(out, open(f"{D}/lists.json", "w", encoding="utf-8"), ensure_ascii=False)
    print("\nlemma lists written:", file=sys.stderr)
    for k, d in out.items():
        top = sorted(d.items(), key=lambda kv: -kv[1])[:12]
        print(f"  {k:10} lemmas={len(d):>7,}  {' '.join(w for w, _ in top)}", file=sys.stderr)


if __name__ == "__main__":
    main()
