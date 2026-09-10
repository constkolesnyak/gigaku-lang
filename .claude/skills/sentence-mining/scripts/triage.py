#!/usr/bin/env python3
"""Judge the candidates the frequency lists cannot: real German word, or noise?

    python3 scripts/triage.py             # judge what is not yet cached
    python3 scripts/triage.py --redo      # ignore the cache and re-judge everything

`analyze.py` sorts every unknown lemma into three fates using `freq/de-yt.tsv` and
`freq/de-words.tsv`. What is in neither list is *not* garbage — measured on one episode, its
201 entries held anime titles (`Punchman`, `Isekai`), English used as-is (`Setup`, `Trigger`,
`hyped`), transcription corpses (`Einsteigeranim`, `Ohrwumm`, `hochwoten`) **and real German
the lists simply lack** (`hochvoten`, `rausballern`, `wegdiskutieren`, `vollgeballern`,
`einpflegen`, `nachanimieren`). Only a reader with the sentence in front of them can tell
those apart, and there are ~1,500 of them across a playlist, so this is a batched `claude -p`
job rather than something read by eye.

Four verdicts, and the fourth is the one that matters: an inflected form of a common word
(`musst`, `krasse`, `Herzen`) is not dropped into a hole — it is appended to the grading
list, because "ungraded ≠ known, and the deliverable is the list to grade" is the standing
rule this whole skill is shaped by.

Follows the repo's measured contract for `claude -p` (lib/claude.py): plain `id: value`
lines matched on the **echoed id, never position**; both counts stated in the request so a
short reply is detectable; whatever did not come back re-asked; the cache written after
every group so an abandoned run keeps what it paid for; `effort` pinned so the answer is not
bimodal.
"""
import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _REPO)
from _config import load_config, work_dir            # noqa: E402
from _german import sort_key                        # noqa: E402
from lib.claude import ask, describe                 # noqa: E402

MODEL = "opus"
EFFORT = "high"
GROUP = 150
RETRIES = 2

KEEP = "K"
VERDICTS = {KEEP: "keep", "DN": "proper noun", "DE": "foreign word",
            "DX": "not a word", "DI": "inflected form → grade instead"}

SYSTEM = """\
You triage German words pulled out of spoken YouTube German for a vocabulary flashcard deck.

The learner is an adult at roughly B2 in German. A card is worth making only for a word that
is a real German dictionary entry he could meet again somewhere else.

For each numbered item you get a WORD and the SENTENCE it was heard in. Answer with exactly
one line per item, `<id>: <verdict>`, using one of these five verdicts and nothing else:

K   — keep. A real German word in its dictionary form: a noun, a full-verb infinitive
      (including separable verbs like `hochvoten`, `rausballern`, `wegdiskutieren`), an
      adjective, an adverb, or a transparent German compound (`Herzensding`, `Gesamtpaket`,
      `Tennisturnier`). Colloquial and youth-slang German counts as German. A German verb
      built on a borrowed stem counts as German if it is inflected as German
      (`hochvoten`, `stretchen`, `speedrunen`).
DN  — a proper noun: an anime, manga, game, film or song title; a character, person,
      channel, studio or brand name.
DE  — a foreign word used as-is, not naturalised German: `Setup`, `Trigger`, `Watch`,
      `Romance`, `hyped`, `overpowert`, `betrayed`. If a German dictionary would list it as
      a normal German loanword, that is K, not DE.
DX  — not a word at all: a mis-transcription, a truncation, two words fused, a typo
      (`Einsteigeranim`, `Ohrwumm`, `hochwoten`, `akzeptiern`, `Goldat`).
DI  — a real German word that should NOT become a card, for either of two reasons:
      (a) this is an inflected, participial or fragmentary form rather than the dictionary
          form (`musst`, `gibst`, `sag`, `achtet`, `krasse`, `coole`, `Herzen`, `Grüße`,
          `lange`, `weiter`, `wandelnd`, `vierter`, `gelangweilen`) — the correct card
          would have to be spelled differently from the WORD you were given; or
      (b) it is everyday German an adult B2 learner is certain to know already — roughly
          the commonest few thousand words (`Danke`, `Junge`, `hängen`, `zurecht`,
          `drüber`, `Sache`, `bleiben`). These are not new vocabulary; they go on a list
          to be marked as known, which is what DI routes them to.

Judge the word as used in ITS sentence. When K and DI are both arguable, prefer DI — a card
spelled wrong is worse than a word deferred. When nothing else fits, prefer DX.

Output only the answer lines. No preamble, no commentary, no blank lines, no markdown.
"""

LINE = re.compile(r"^\D{0,4}(\d{6,})\s*[:.\-\t]\s*([KD][A-Z]?)\b", re.M)


def note(msg):
    print(msg, file=sys.stderr, flush=True)


def load_cache(path):
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["word"]: r["verdict"] for r in csv.DictReader(f, delimiter="\t") if r.get("word")}


def save_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        f.write("word\tverdict\n")
        for w, v in sorted(cache.items()):
            f.write(f"{w}\t{v}\n")


def judge(items):
    """items: [(id, word, sentence)] → {id: verdict}. Re-asks whatever did not come back."""
    out = {}
    pending = list(items)
    for attempt in range(RETRIES + 1):
        if not pending:
            break
        body = "\n".join(f"{i}: {w} — {s}" for i, w, s in pending)
        text = (f"{len(pending)} items. Answer with exactly {len(pending)} lines, "
                f"one `<id>: <verdict>` per item, ids echoed exactly as given.\n\n{body}")
        reply, usage, cost = ask(text, SYSTEM, MODEL, what="triage", effort=EFFORT)
        got = {int(m.group(1)): m.group(2) for m in LINE.finditer(reply)}
        wanted = {i for i, _, _ in pending}
        fresh = {i: v for i, v in got.items() if i in wanted and v in VERDICTS}
        out.update(fresh)
        note(f"    {len(fresh)}/{len(pending)} answered  [{describe(usage)}]  ${cost:.3f}")
        pending = [it for it in pending if it[0] not in out]
        if pending and attempt < RETRIES:
            note(f"    re-asking {len(pending)} that did not come back")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--group", type=int, default=GROUP)
    a = ap.parse_args()

    cfg = load_config()
    wd = work_dir(cfg)
    # Read the FULL candidate set and write a separate file. Writing the capped result back
    # over candidates.json made a second run start from the previous run's 400 instead of the
    # 1,418 analyze.py found, so re-running could only ever shrink the deck.
    cpath, tpath = wd / "candidates.json", wd / "triage.tsv"
    opath = wd / "triaged.json"
    data = json.loads(cpath.read_text(encoding="utf-8"))
    cache = {} if a.redo else load_cache(tpath)

    # Judge everything a *rank* does not vouch for. `de-yt.tsv` is rank-ordered over real
    # spoken YouTube German, so a rank is evidence. Bare membership in `de-words.tsv` is not:
    # it is a dictionary-shaped list that genuinely contains `Hole` (лунка), `Synchro`
    # (озвучка) and `wandelnd` (ходячий), and trusting it let 26 unjudged cards through.
    odd = [c for c in data["candidates"] if c["bucket"] == "odd" or not c.get("yt_rank")]
    todo = [c for c in odd if c["lemma"] not in cache]
    note(f"{len(odd)} candidates no rank vouches for, {len(todo)} to judge "
         f"({len(odd) - len(todo)} cached), groups of {a.group}, model {MODEL}/{EFFORT}")

    for start in range(0, len(todo), a.group):
        chunk = todo[start:start + a.group]
        note(f"  group {start // a.group + 1}: {len(chunk)} words")
        items = [(100000 + start + i, c["lemma"], c["sentence"]) for i, c in enumerate(chunk)]
        verdicts = judge(items)
        for (i, w, _), c in zip(items, chunk):
            cache[w] = verdicts.get(i, "DX")   # never answered twice → treat as noise
        save_cache(tpath, cache)               # persist per group, not at the end

    kept, dropped, deferred = [], [], []
    for c in data["candidates"]:
        needs = c["bucket"] == "odd" or not c.get("yt_rank")
        v = cache.get(c["lemma"], KEEP) if needs else KEEP
        if v == KEEP:
            kept.append(c)
        elif v == "DI":
            c["dropped"] = VERDICTS[v]
            deferred.append(c)
        else:
            c["dropped"] = VERDICTS[v]
            dropped.append(c)

    # No cap here. Triage answers "is this a real German word"; how many cards the deck
    # gets is decided after clarity.py has seen which sentences actually teach, because
    # capping on usefulness first would keep words whose only sentence reveals nothing.
    kept.sort(key=sort_key)
    data["candidates"] = kept
    data["triaged_out"] = dropped
    data["triaged_to_grade"] = deferred
    opath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # DI words are not lost: they are exactly the "mark this in Migaku" case.
    gpath = wd / "grade-me.tsv"
    if deferred and gpath.exists():
        with open(gpath, "a", encoding="utf-8") as f:
            for c in deferred:
                f.write(f"{c['lemma']}\t\t\t\t1\t{c['sentence']}\n")

    from collections import Counter
    tally = Counter(cache[c["lemma"]] for c in odd if c["lemma"] in cache)
    note("\n  " + ", ".join(f"{VERDICTS[k]}={v}" for k, v in tally.most_common()))
    note(f"  candidates after triage: {len(kept)}  "
         f"(dropped {len(dropped)}, sent to grading {len(deferred)})")
    note(f"  wrote {opath}")


if __name__ == "__main__":
    main()
