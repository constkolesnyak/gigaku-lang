#!/usr/bin/env python3
"""Pick each word's sentence by what it TEACHES, and drop the words no sentence teaches.

    python3 scripts/clarity.py                 # score, choose, write chosen.json
    python3 scripts/clarity.py --floor 60      # stricter
    python3 scripts/clarity.py --redo          # ignore the cached scores

Until this existed, a word's sentence was chosen by "fewest unknown words, then a length
penalty". That is a proxy for *difficulty*, not for *teaching*, and it picked exactly the
card `lib/anki/prompt_de.py` warns is the most common one in this collection: fluent,
well-formed and entirely empty. `Das macht man zurecht.` is short, clean, i+1 — and reveals
nothing about `zurecht`.

So the choice is made by the repo's own German clarity rubric, unchanged: *blank the target
out, then ask how many words could fill the hole*. It is calibrated on this exact corpus —
transcribed conversational speech, ASR-split clauses, fillers — and its anchors are real
cards from this collection.

Two things follow from using it:

- **It chooses.** `analyze.py` now offers every sentence a word was heard in, plus spans
  glued to a neighbour, and `prompt.render` lays a word's candidates side by side under one
  heading so the judgement is comparative — which is what the score is used for and what
  models are steady at.
- **It rejects.** Half the words in this playlist occur once. When that one sentence
  reveals nothing, the honest outcome is no card, not a bad card. `--floor` is where that
  line sits.
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _REPO)
from _config import load_config, work_dir           # noqa: E402
from _german import sort_key                        # noqa: E402
from lib.claude import ask, describe                # noqa: E402
from lib.anki import prompt, prompt_de              # noqa: E402

MODEL, EFFORT, GROUP = "opus", "high", 150
BAND = 5          # clarity points within which a cheaper sentence may be preferred
MIN_CONTEXT = 3   # content words that must survive blanking the target, whatever the score
MAX_CLIP_MS = 10000   # hard ceiling: a longer card is not wanted, however clear it is
BASE_ID = 100000


def note(m):
    print(m, file=sys.stderr, flush=True)


def load_scores(path):
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {(r["word"], r["sentence"]): int(r["score"])
                for r in csv.DictReader(f, delimiter="\t") if r.get("sentence")}


def save_scores(path, scores):
    with open(path, "w", encoding="utf-8") as f:
        f.write("word\tscore\tsentence\n")
        for (w, sent), sc in sorted(scores.items()):
            f.write(f"{w}\t{sc}\t{sent}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=int, default=50,
                    help="lowest clarity a card may have (default %(default)s). The rubric's "
                         "own bands: 0-10 the slot takes anything, 15-25 part of speech only, "
                         "30-45 category only, 50-65 a small family of meanings, 70-85 one "
                         "idea survives, 90-100 the sentence gives the meaning")
    ap.add_argument("--min-context", type=int, default=MIN_CONTEXT,
                    help="content words that must remain once the target is blanked out "
                         "(default %(default)s)")
    ap.add_argument("--max-clip", type=int, default=MAX_CLIP_MS,
                    help="hard ceiling on the clip length in ms (default %(default)s). "
                         "A longer card is not taken even if its clarity is the highest: "
                         "measured 2026-09-09, the median was 8.8 s with a maximum of 15.5 s, "
                         "and the long ones are splices with a neighbouring sentence")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--group", type=int, default=GROUP)
    a = ap.parse_args()

    cfg = load_config()
    wd = work_dir(cfg)
    data = json.loads((wd / "triaged.json").read_text(encoding="utf-8"))
    cands = data["candidates"]
    spath = wd / "clarity.tsv"

    cached = {} if a.redo else load_scores(spath)
    scores = dict(cached)

    # (id, sentence, morph) — prompt.render blocks by morph, so a word's options are laid
    # side by side and judged against each other rather than placed on an absolute scale.
    items, back = [], {}
    for c in cands:
        for oi, opt in enumerate(c["options"]):
            if (c["lemma"], opt["sentence"]) in cached:
                continue
            i = BASE_ID + len(items)
            items.append((i, opt["sentence"], c["lemma"]))
            back[i] = (c["lemma"], opt["sentence"])
    note(f"{len(cands)} words, {sum(len(c['options']) for c in cands)} sentences, "
         f"{len(items)} to score ({len(cached)} cached), rubric {prompt_de.FINGERPRINT}")

    if items:
        groups = prompt.groups(items, a.group)
        for gi, group in enumerate(groups, 1):
            reply, usage, cost = ask(prompt.render(group), prompt_de.SYSTEM, model=MODEL,
                                     what=f"clarity {gi}/{len(groups)}", effort=EFFORT)
            got = prompt.parse(reply, [i for i, _, _ in group])
            for i, sc in got.items():
                scores[back[i]] = sc
            save_scores(spath, scores)          # after every group, never only at the end
            note(f"  {gi}/{len(groups)}: {len(got)}/{len(group)} · {describe(usage)}")

    # Choose: the clearest sentence a word has, if it clears the floor.
    chosen, rejected, hist = [], [], Counter()
    for c in cands:
        ranked = sorted(((scores.get((c["lemma"], o["sentence"]), 0), oi)
                         for oi, o in enumerate(c["options"])), reverse=True)
        best = ranked[0][0]
        hist[min(best // 10 * 10, 90)] += 1
        # Take the CLEAREST sentence; use "fewer unknown words" only to separate options
        # that are near-equally clear (within BAND points).
        #
        # An earlier version took the easiest of everything above the floor, on the theory
        # that clarity is a threshold. It is not: `Gauner` had a span scoring 78 ("...der in
        # Hollywood lebt und Leute austrickst") and the bare "Also er ist quasi so ein so ein
        # Gauner." at 66, and the easiest-wins rule shipped the 66 — a card whose sentence
        # restates the word and teaches nothing, which is exactly the defect this whole pass
        # exists to remove. Cheapness is worth a point or two of clarity, never twelve.
        # Eligibility before scoring: a sentence with nothing left once the target is blanked
        # cannot teach it, and the score is not allowed to overrule that. A word falls back to
        # another of its sentences rather than being dropped outright.
        def dur(o):
            return o.get("clip_end_ms", o["sentence_end_ms"]) - o.get("clip_start_ms", o["sentence_start_ms"])
        passing = [(s_, oi) for s_, oi in ranked
                   if s_ >= a.floor
                   and c["options"][oi].get("context", 0) >= a.min_context
                   and dur(c["options"][oi]) <= a.max_clip]
        if passing:
            top = passing[0][0]
            near = [(s_, oi) for s_, oi in passing if s_ >= top - BAND]
            oi = min(near, key=lambda t: (c["options"][t[1]]["unknown_count"],
                                          c["options"][t[1]].get("span", 1), -t[0]))[1]
            best = scores.get((c["lemma"], c["options"][oi]["sentence"]), 0)
            eligible = True
        else:
            # Nothing both clears the floor and survives the blank test → no card. Falling
            # back to the best-scoring option here quietly re-admitted the exact sentences the
            # blank test exists to refuse: `vorarbeiten`'s only option over the floor was
            # "Wir werden bei Platz 8 anfangen und uns vorarbeiten." (2 content words left,
            # scored 62), while every option with real context scored 38-44.
            oi = ranked[0][1]
            eligible = False
        pick = dict(c["options"][oi])
        pick.update({k: c[k] for k in ("bucket", "yt_rank", "hits", "ru") if k in c})
        pick["clarity"] = best
        pick["clarity_rank"] = [s for s, _ in ranked]
        (chosen if eligible and best >= a.floor else rejected).append(pick)

    note("\n  clarity of each word's best sentence:")
    for band in sorted(hist):
        note(f"    {band:>3}-{band+9:<3} {'#' * (hist[band] * 60 // max(hist.values()))} {hist[band]}")

    # One sentence per card: Anki dedupes 🇩🇪 notes on `Sentence`, its first field.
    chosen.sort(key=lambda c: (-c["clarity"], sort_key(c)))
    seen, final = set(), []
    for c in chosen:
        if c["sentence"] in seen:
            continue
        seen.add(c["sentence"])
        final.append(c)
    cap = cfg.get("cap") or 0
    if cap and len(final) > cap:
        note(f"  capping {len(final)} → {cap}")
        final = final[:cap]

    data["candidates"] = final
    data["rejected_low_clarity"] = rejected
    data["floor"] = a.floor
    (wd / "chosen.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"\n  kept {len(final)} (clarity ≥ {a.floor}), rejected {len(rejected)}, "
         f"{len(chosen) - len(final)} lost to a duplicate sentence")
    note(f"  wrote {wd / 'chosen.json'}")


if __name__ == "__main__":
    main()
