#!/usr/bin/env python3
"""Turn a ranked frequency list into a deck word list: subtract, filter, gloss.

    uv run python freqdeck/wordlist.py --source yt --n 1000

Writes `freq/de-yt-deck.tsv` (`Wort⇥перевод`, in the source's own rank order) — the input
`deck.py --list yt` builds cards from — plus `freqdeck/out/yt-verdicts.tsv`, which keeps
every verdict INCLUDING the rejects with their reason, so the filter can be checked by
what it removes rather than trusted (the lesson `known.py` learned the hard way: a
cognate match unreviewed would have cut `links`, `First`, `Fund`).

Three subtractions, then one judgement:

  ungraded  Migaku has met the word and never graded it. The freq pipeline counts any
            Migaku row as known; the YouTube pipeline keeps ungraded rows and ranks them
            first, which is why its head reads `eigentlich, eben, ob, sondern`. The user's
            decision (2026-08-27): **ungraded counts as known** — 2,409 rows, 438 of the
            top 500. Without this the first cards would teach `ob`.
  known     `gigaku words --lang de --stage known`.
  built     words the deck already has (freqdeck/out/chosen.tsv), so a second list only
            ever adds.

  the judgement  A ranked list of subtitle tokens carries English intrusions (`me`,
            `random`, `Content`, `beach`), lowercased proper names (`picasso`, `ägypten`),
            abbreviations (`afd`, `ai`) and bare forms (`ums`, `übersetzt`). The German
            Wiktionary lemma table cannot separate them — measured 2026-08-27: `me`,
            `My`, `random`, `easy`, `Content`, `Green` all have German entries. So the
            call is made by Opus, one line per word, together with the Russian gloss the
            sentence rubric needs: dictionary glosses (the source's own `ru` column, then
            de-ru.tsv / de-ru-fill.tsv) are offered to it and it may correct them.
"""
import argparse
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import deck  # noqa: E402
from lib.claude import ask, describe  # noqa: E402

SOURCES = {
    # name: (ranked tsv with a header, its word column, its gloss column, its Migaku column, output list)
    "yt": ("freq/de-yt.tsv", "word", "ru", "migaku", "freq/de-yt-deck.tsv"),
}
GROUP = 250

SYSTEM = """\
Ты отбираешь немецкие слова для карточек и даёшь им короткий русский перевод.

Каждая строка — `id: слово (перевод из словаря или —)`. Слова взяты из субтитров немецких
YouTube-каналов, поэтому среди них есть мусор. Ответь одной строкой на слово:

- `id: KEEP | перевод` — это немецкое слово, которое имеет смысл учить. Перевод: 1–3
  русских эквивалента через запятую, самое употребительное значение первым, без пояснений
  и без транскрипции. Словарный перевод, если он есть, обычно и оставляй; исправь, если он
  неверен или называет редкое значение.
- `id: SKIP | причина` — учить нечего. Причины (одним словом):
  `english` — английское слово, попавшее в немецкую речь (me, random, Content, beach);
  `name` — имя собственное, город, страна, бренд, партия (picasso, ägypten, afd);
  `abbrev` — сокращение или буквы (ai, ARD);
  `form` — не словарная форма, а склонение/спряжение/сращение с артиклем (ums, übersetzt);
  `junk` — междометие-описание звука, обрывок, опечатка распознавания.

Немецкое слово английского происхождения, которое немцы реально употребляют как немецкое
(`Abo`, `streamen`, `Streamer`, `Challenge`, `Review`, `hypen`, `Döner`), — это KEEP,
а не english: критерий в том, склоняется ли оно и говорят ли его в немецкой фразе.
Разговорное и грубое — тоже KEEP.

Только строки `id: KEEP | перевод` или `id: SKIP | причина`, по одной на слово, ничего
больше.
"""

_LINE = re.compile(r"^\s*(\d+)\s*[:.]\s*(KEEP|SKIP)\s*\|\s*(\S.*?)\s*$", re.M | re.I)


def glosses():
    """Dictionary glosses from the frequency pipeline's caches."""
    out = {}
    for path in ("freq/de-ru.tsv", "freq/de-ru-fill.tsv"):
        p = os.path.join(ROOT, path)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8"):
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2 and parts[1]:
                out.setdefault(parts[0], parts[1])
    return out


def candidates(source):
    path, wcol, rucol, migcol, _ = SOURCES[source]
    rows = list(csv.DictReader(open(os.path.join(ROOT, path), encoding="utf-8"), delimiter="\t"))
    known = deck.known_words()
    built = {r[0] for r in deck.read_tsv(deck.CHOSEN)}
    cache = glosses()
    pool, dropped = [], {"ungraded": 0, "known": 0, "built": 0}
    for r in rows:
        w = r[wcol].strip()
        if not w:
            continue
        if (r.get(migcol) or "").strip() == "ungraded":
            dropped["ungraded"] += 1
            continue
        if w.lower() in known:
            dropped["known"] += 1
            continue
        if w in built:
            dropped["built"] += 1
            continue
        gloss = (r.get(rucol) or "").strip() or cache.get(w) or cache.get(w.lower()) or cache.get(w.capitalize()) or ""
        pool.append((w, gloss))
    return pool, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="yt", choices=sorted(SOURCES))
    ap.add_argument("--n", type=int, default=1000, help="how many keepers the list should hold")
    ap.add_argument("--effort", default="low")
    a = ap.parse_args()
    out_list = os.path.join(ROOT, SOURCES[a.source][4])
    verdicts_path = os.path.join(deck.OUT, f"{a.source}-verdicts.tsv")   # word ⇥ KEEP|SKIP ⇥ gloss|reason

    pool, dropped = candidates(a.source)
    deck.note(f"{a.source}: {len(pool)} candidates "
              f"(subtracted: ungraded {dropped['ungraded']}, known {dropped['known']}, already in the deck {dropped['built']})")
    seen = {r[0]: (r[1], r[2] if len(r) > 2 else "") for r in deck.read_tsv(verdicts_path)}
    keepers = [(w, seen[w][1]) for w, _ in pool if seen.get(w, ("",))[0] == "KEEP"]

    i = 0
    while len(keepers) < a.n and i < len(pool):
        batch = []
        while i < len(pool) and len(batch) < GROUP:
            w, gloss = pool[i]
            i += 1
            if w not in seen:
                batch.append((w, gloss))
        if not batch:
            continue
        text = "\n".join(f"{j}: {w} ({g or '—'})" for j, (w, g) in enumerate(batch, 1))
        reply, usage, cost = ask(text, SYSTEM, model=deck.MODEL, what="wordlist filter", effort=a.effort)
        rows = []
        for m in _LINE.finditer(reply or ""):
            j = int(m.group(1))
            if 1 <= j <= len(batch):
                w = batch[j - 1][0]
                if w in seen:
                    continue
                verdict, tail = m.group(2).upper(), m.group(3)
                seen[w] = (verdict, tail)
                rows.append((w, verdict, tail))
        deck.append_tsv(verdicts_path, rows)
        kept = [(w, t) for w, v, t in rows if v == "KEEP"]
        keepers = [(w, seen[w][1]) for w, _ in pool if seen.get(w, ("",))[0] == "KEEP"]
        deck.note(f"  {len(rows)}/{len(batch)} judged, +{len(kept)} keep → {len(keepers)} in total · "
                  f"{describe(usage)} · ${cost:.2f}")

    keepers = keepers[:a.n]
    with open(out_list + ".part", "w", encoding="utf-8") as f:
        for w, gloss in keepers:
            f.write(f"{w}\t{gloss}\n")
    os.replace(out_list + ".part", out_list)
    skipped = [(w, seen[w][1]) for w, _ in pool if seen.get(w, ("",))[0] == "SKIP"]
    by_reason = {}
    for w, reason in skipped:
        by_reason.setdefault(reason.split()[0].lower().strip(".,"), []).append(w)
    deck.note(f"\n{len(keepers)} words → {out_list}; {len(skipped)} rejected (check by eye):")
    for reason, ws in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        deck.note(f"  {len(ws):4d} {reason:9} {', '.join(ws[:18])}")


if __name__ == "__main__":
    main()
