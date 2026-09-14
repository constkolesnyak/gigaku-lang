#!/usr/bin/env python3
"""Final check of the Frequency 🇩🇪 deck — every field filled, and filled well.

    uv run --with simplemma python freqdeck/audit.py            # mechanical + Opus review
    uv run --with simplemma python freqdeck/audit.py --no-llm   # mechanical only

Mechanical (no model): for every note in the deck — Sentence, Word, Word Audio,
Definition, Definition Audio, Notes, Sentence Audio all non-empty; Definition is exactly
one `bilingual` block and no `monolingual` one; every [audio:] file exists in
collection.media and is not tiny; Notes is Cyrillic; the sentence contains the target
(a simplemma lemma of some token equals the word, or the word's stem for separable
verbs); am-all-morphs-count is filled (recalc ran). Every failure is listed by word.

Quality (Opus, lib/claude.py, 100 notes a request): a reviewer reads word, sentence,
definition and translation together and answers `id: OK` or `id: <category> — <what>`:
  SENSE   the English definition does not match the sense the sentence uses
  DEF     the definition is not a plain one-sentence English gloss (German in it, a list,
          markdown, restates the sentence, >2 sentences)
  RU      the Russian translation is wrong, unnatural, or drops/changes the target's meaning
  DE      the German sentence is unnatural, ungrammatical, or does not really use the word
Findings go to freqdeck/out/audit.tsv (word ⇥ category ⇥ note) and are printed; OK lines are
counted only. The reviewer is asked to be strict on SENSE and RU and lenient on style.
"""
import argparse
import html
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import deck  # noqa: E402
from lib.claude import ask, describe  # noqa: E402

AUDIT = os.path.join(deck.OUT, "audit.tsv")
GROUP = 100

SYSTEM = """\
You review German flashcards for a Russian-speaking learner. Each item has an id, the
TARGET word, the German SENTENCE that teaches it, the English DEFINITION of the target as
used on the card, and the Russian TRANSLATION of the sentence.

Answer one line per item: `id: OK`, or `id: CATEGORY — what is wrong` (one short clause,
English). Categories, in order of importance:

- SENSE — the definition explains a different sense of the target than the one the
  sentence uses (Gericht = court vs dish), or a different word.
- RU — the Russian translation is wrong, unnatural, or loses/changes the target's
  meaning. Free, idiomatic Russian is fine; a wrong meaning is not.
- DE — the German sentence is unnatural or ungrammatical for a native speaker, or the
  target does not actually occur in it in the stated sense.
- DEF — the definition is not a plain English gloss: German words in it, a list of
  senses, markdown, more than two sentences, or it merely restates the sentence.

Be strict on SENSE and RU, lenient on style. Do not flag a definition for being short.
Every item exactly once, in order, nothing else in the reply.
"""


def strip(field):
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<!--.*?-->", "", field, flags=re.S))).strip()


def mechanical(notes, present, lemmatize):
    media = os.path.expanduser("~/Library/Application Support/Anki2/MainProfile/collection.media")
    f = lambda n, k: n["fields"][k]["value"]
    problems, by_hand = [], []
    for n in notes:
        w = f(n, "Word").strip()
        for k in ("Sentence", "Word", "Word Audio", "Definition", "Definition Audio", "Notes", "Sentence Audio"):
            if not f(n, k).strip():
                problems.append((w, f"{k} empty"))
        d = f(n, "Definition")
        if d.count('def-type="bilingual"') != 1 or "monolingual" in d:
            problems.append((w, "Definition is not exactly one bilingual block"))
        if "monolingual" in f(n, "Definition Audio"):
            problems.append((w, "Definition Audio carries a monolingual block"))
        for k in ("Word Audio", "Definition Audio", "Sentence Audio"):
            tags = re.findall(r"\[audio:(.+?)\]", f(n, k))
            if f(n, k).strip() and not tags:
                problems.append((w, f"{k} has no [audio:] tag"))
            for t in tags:
                if t not in present:
                    problems.append((w, f"{k} file missing: {t}"))
                elif os.path.getsize(os.path.join(media, t)) < 2000:
                    problems.append((w, f"{k} file tiny: {t}"))
        if not re.search(r"[А-Яа-яЁё]", f(n, "Notes")):
            problems.append((w, "Notes is not Russian"))
        s = f(n, "Sentence")
        toks = re.findall(r"[A-Za-zÄÖÜäöüß]+", s)
        forms = {t.lower() for t in toks} | {lemmatize(t).lower() for t in toks}
        wl = w.lower()
        stem = re.sub(r"^(" + "|".join(deck.PREFIXES) + ")", "", wl) if len(wl) > 6 else wl
        if not (wl in forms or lemmatize(w).lower() in forms or any(stem and fo.startswith(stem[:5]) for fo in forms)):
            # A separable verb splits (Mach … an), a participle ablauts (abgebogen), U-Bahn
            # tokenises apart — no lemmatiser here can settle those, so they are listed
            # for a reader rather than counted as failures (all 18 of the first run were
            # read and were fine).
            participle = any(t.lower().startswith("ge") and t.lower()[2:4] == wl[:2] for t in toks)
            if stem != wl or "-" in w or lemmatize(w).lower().endswith("en") or participle:
                by_hand.append((w, s))
            else:
                problems.append((w, f"target not found in sentence: {s}"))
        if not f(n, "am-all-morphs-count").strip():
            problems.append((w, "am-all-morphs-count empty (recalc?)"))
    return problems, by_hand


def review(notes):
    f = lambda n, k: n["fields"][k]["value"]
    items = [(100000 + i, f(n, "Word").strip(), f(n, "Sentence").strip(),
              strip(f(n, "Definition")), strip(f(n, "Notes"))) for i, n in enumerate(notes, 1)]
    findings, ok = [], 0
    groups = [items[i:i + GROUP] for i in range(0, len(items), GROUP)]
    for gi, g in enumerate(groups, 1):
        text = f"{len(g)} items — answer with {len(g)} lines.\n\n" + "\n\n".join(
            f"ID: {i}\nTARGET: {w}\nSENTENCE: {s}\nDEFINITION: {d}\nTRANSLATION: {t}"
            for i, w, s, d, t in g)
        reply, usage, cost = ask(text, SYSTEM, model="opus", what=f"audit {gi}/{len(groups)}", effort="high")
        got = {}
        for m in re.finditer(r"^\s*(\d{6})\s*[:.]\s*(.+?)\s*$", reply or "", re.M):
            got.setdefault(int(m.group(1)), m.group(2))
        by_id = {i: (w, s, d, t) for i, w, s, d, t in g}
        for i, verdict in got.items():
            if i not in by_id:
                continue
            if verdict.strip().upper().startswith("OK"):
                ok += 1
            else:
                cat, _, what = verdict.partition("—")
                findings.append((by_id[i][0], cat.strip(), what.strip() or verdict))
        missing = len(g) - len([i for i in got if i in by_id])
        deck.note(f"  audit {gi}/{len(groups)}: {ok} ok so far, {len(findings)} findings"
                  f"{f', {missing} unanswered' if missing else ''} · {describe(usage)} · ${cost:.2f}")
    return findings, ok


def main():
    import simplemma
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--words", help="comma list: review only these words (mechanical still runs on all)")
    a = ap.parse_args()
    lemmatize = lambda w: simplemma.lemmatize(w, lang="de")
    media = os.path.expanduser("~/Library/Application Support/Anki2/MainProfile/collection.media")
    present = set(os.listdir(media))
    notes = deck.anki("notesInfo", notes=deck.anki("findNotes", query=f'deck:"{deck.DECK}"'))
    print(f"{len(notes)} notes in {deck.DECK}")
    problems, by_hand = mechanical(notes, present, lemmatize)
    print(f"mechanical: {len(problems)} problem(s), {len(by_hand)} sentence(s) to read by hand "
          "(separable verb / participle — the machine can't confirm the target is in them)")
    for w, p in problems:
        print(f"  {w:16} {p}")
    for w, s in by_hand:
        print(f"  ?  {w:14} {s}")
    if a.no_llm:
        return
    if a.words:
        wanted = {w.strip() for w in a.words.split(",")}
        notes = [n for n in notes if n["fields"]["Word"]["value"].strip() in wanted]
    findings, ok = review(notes)
    with open(AUDIT + (".recheck" if a.words else ""), "w", encoding="utf-8") as fh:
        for w, cat, what in findings:
            fh.write(f"{w}\t{cat}\t{what}\n")
    print(f"\nreview: {ok} OK, {len(findings)} finding(s) → {AUDIT + ('.recheck' if a.words else '')}")
    for w, cat, what in findings:
        print(f"  {w:16} {cat:6} {what}")


if __name__ == "__main__":
    main()
