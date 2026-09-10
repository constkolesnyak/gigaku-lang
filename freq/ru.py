"""Russian glosses for the study list, from two Wiktionaries (2026-08-25).

The user asked for a Russian translation on every word of the main list, from a
dictionary if possible — so this is dictionaries, not a model: de.wiktionary's own
Übersetzungen sections carry Russian for the well-tended pages (`Junge → мальчик`),
and ru.wiktionary describes German headwords in Russian outright (`Koffer → чемодан;
сундук; кофр`). The de side wins where both exist — a translation line is terser than
a definition — and the ru side fills what de never translated. ru.wiktionary gives
inflected forms their own entries whose "gloss" is a grammar sentence («форма
настоящего времени…»), so those are recognised and skipped, sense by sense.

Reads de-living-german.tsv, writes de-words.tsv as `Wort<TAB>перевод` (the column is
empty where neither dictionary answers — an honest gap, not a guess) and de-ru.tsv as
the reusable word→gloss table so a rebuild does not re-stream 600 MB of dumps.
"""
import gzip
import json
import os
import re
import sys
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
FORMY = re.compile(r"^форма |наклонения глагола|^уменьш[.-]|причастие|деепричастие"
                   r"|множественного числа существительного|единственного числа"
                   r" существительного|степень сравнения")


def clean(g):
    # de.wiktionary appends romanisations to its Russian translations —
    # "дождь идти (dožd’ idët)" — a parenthetical with no Cyrillic inside is one
    g = re.sub(r"\(([^)]*)\)", lambda m: "" if not re.search(r"[а-яё]", m.group(1), re.I) else m.group(0), g)
    g = re.sub(r"\s+", " ", g).strip(" ;,")
    return g


def main():
    words = []
    for line in open(f"{D}/de-living-german.tsv", encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if p[0] != "rank":
            words.append(p[1])
    wanted = set(words) | {w.lower() for w in words} | {w.capitalize() for w in words}

    de_ru = defaultdict(list)          # word -> [ru translations] (de.wiktionary)
    with gzip.open(f"{D}/dewikt-raw.jsonl.gz", "rt", encoding="utf-8",
                   errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            w = e.get("word")
            if e.get("lang_code") != "de" or w not in wanted:
                continue
            for t in e.get("translations") or ():
                if t.get("lang_code") == "ru":
                    ru = clean(t.get("word") or "")
                    if ru and ru not in de_ru[w] and len(de_ru[w]) < 4:
                        de_ru[w].append(ru)

    ru_gloss = {}                      # word -> first real gloss (ru.wiktionary)
    with gzip.open(f"{D}/ruwikt-raw.jsonl.gz", "rt", encoding="utf-8",
                   errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            w = e.get("word")
            if e.get("lang_code") != "de" or w not in wanted or w in ru_gloss:
                continue
            for s in e.get("senses") or ():
                for g in s.get("glosses") or ():
                    g = clean(g)
                    if g and not FORMY.search(g):
                        ru_gloss[w] = g
                        break
                if w in ru_gloss:
                    break

    fill = {}                      # claude-CLI gap fill (ru_fill.py), lowest priority
    if os.path.exists(f"{D}/de-ru-fill.tsv"):
        for line in open(f"{D}/de-ru-fill.tsv", encoding="utf-8"):
            p = line.rstrip("\n").split("\t")
            if len(p) == 2 and p[1]:
                fill.setdefault(p[0], p[1])

    def gloss(w):
        for k in (w, w.lower(), w.capitalize()):
            if de_ru.get(k):
                return ", ".join(de_ru[k])
        for k in (w, w.lower(), w.capitalize()):
            if ru_gloss.get(k):
                return ru_gloss[k]
        return fill.get(w, "")

    n = 0
    with open(f"{D}/de-ru.tsv", "w", encoding="utf-8") as f:
        for w in words:
            g = gloss(w)
            n += bool(g)
            f.write(f"{w}\t{g}\n")
    with open(f"{D}/de-words.tsv", "w", encoding="utf-8") as f:
        for line in open(f"{D}/de-ru.tsv", encoding="utf-8"):
            f.write(line)
    print(f"{len(words):,} words · {n:,} glossed ({n * 100 // len(words)}%) · "
          f"de-side {sum(1 for w in words if any(de_ru.get(k) for k in (w, w.lower(), w.capitalize()))):,}",
          file=sys.stderr)
    miss = [w for w in words[:3000] if not gloss(w)]
    print(f"gaps in top-3000: {len(miss)} → {', '.join(miss[:40])}", file=sys.stderr)


if __name__ == "__main__":
    main()
