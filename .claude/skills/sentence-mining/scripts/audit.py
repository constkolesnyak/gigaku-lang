#!/usr/bin/env python3
"""The closing gate: is each pushed card actually teachable?

    python3 scripts/audit.py                # mechanical + Opus review → mining/audit.tsv
    python3 scripts/audit.py --no-llm       # mechanical only
    python3 scripts/audit.py --apply        # suspend + tag whatever the review flagged

`triage.py` judges the *word*. This judges the **card**, and it exists because the two can
fail independently: `Maß` is a real German word with a real frequency rank, so triage passed
it, but the sentence it was mined from — "dieser Maß ist mittlerweile von Menschen belebt" —
is YouTube mishearing "dieser **Mars**". The word is fine; the card is not.

This is `freqdeck/audit.py`'s idea with a rubric rewritten for mined cards. That one judges
*generated* sentences and calls a sentence unnatural or ungrammatical a defect. These
sentences are transcribed speech from a YouTuber talking fast, so colloquial, elliptical and
ungrammatical is what correct input looks like here, and flagging it would flag most of the
deck. What is a defect is the transcription being wrong, or the word not really being in the
sentence at all.

`--apply` suspends and tags. It does not delete: a suspended card is one click from coming
back, and every Anki-affecting step here has to be individually revertible.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _REPO)
from _config import load_config, deck_main, deck_deferred, work_dir   # noqa: E402
from _anki import anki_request                                        # noqa: E402
from lib.claude import ask, describe                                  # noqa: E402

MODEL, EFFORT, GROUP = "opus", "high", 100
MEDIA = os.path.expanduser("~/Library/Application Support/Anki2/MainProfile/collection.media")
MIN_MP3 = 2000
CATEGORIES = {"ASR", "MISSING", "SENSE", "CONTEXT"}

SYSTEM = """\
You review German vocabulary flashcards that were mined from spoken YouTube video. Each item
gives an id, the target WORD, the SENTENCE it was heard in, and the English DEFINITION.

The sentences are automatic transcriptions of a German YouTuber talking fast about anime.
**Spoken German is the point.** Colloquial wording, slang, filler, ellipsis, a missing verb, a
sentence that starts mid-thought, an anime title you do not recognise — none of these are
defects. Do not flag informality, register, or subject matter. Do not flag a sentence for
being about anime.

Flag a card only for one of these four, and prefer OK whenever you are unsure:

ASR      the transcription is wrong where it matters: the target word is clearly a
         mishearing of a different word, or the sentence is garbled into nonsense around it.
         Example: WORD `Maß` in "dieser Maß ist mittlerweile von Menschen belebt" — the
         speaker said "Mars".
MISSING  the target word does not actually occur in the sentence in any form — not as an
         inflection, not as the two halves of a separable verb. The card would show a word
         its own example never uses.
SENSE    the English definition describes a different sense of the word than the sentence
         uses, so the card teaches the wrong meaning.
CONTEXT  the sentence cannot be understood at all without having watched the video — it is
         only a pronoun or a reference with no content of its own. A sentence that merely
         mentions a show by name is fine; this is for sentences that say nothing.

An item whose DEFINITION is empty has not been enriched yet — that is normal, it is how this
runs before the expensive fields are paid for. Judge such an item on ASR, MISSING and CONTEXT
only, and never answer SENSE for it.

Answer with exactly one line per item and nothing else:
  `<id>: OK`
  `<id>: <CATEGORY> — <at most 12 words on what is wrong>`
"""

LINE = re.compile(r"^\D{0,4}(\d{6,})\s*[:.\-\t]\s*(OK|ASR|MISSING|SENSE|CONTEXT)\b[ \t]*[—-]?[ \t]*(.*)$", re.M)


def note(msg):
    print(msg, file=sys.stderr, flush=True)


def strip_html(s):
    return re.sub(r"<[^>]+>", "", re.sub(r"<!--.*?-->", "", s, flags=re.S)).strip()


def load_notes(cfg):
    decks = [d for d in (deck_main(cfg), deck_deferred(cfg)) if d]
    q = "(" + " OR ".join(f'deck:"{d}"' for d in decks) + ")"
    ids = anki_request("findNotes", query=q)
    out = []
    for i in range(0, len(ids), 500):
        out.extend(anki_request("notesInfo", notes=ids[i:i + 500]))
    return out


def mechanical(notes):
    """Everything a rule can check, so the model is only asked what needs judgement."""
    present = set(os.listdir(MEDIA))
    bad = []
    for n in notes:
        f = {k: v["value"] for k, v in n["fields"].items()}
        word = f["Word"].strip()
        # Definition/Definition Audio are deliberately NOT required: the audit runs before
        # enrichment, so that only surviving cards are paid for.
        for label, val in (("Word", word), ("Sentence", f["Sentence"].strip()),
                           ("Sentence Audio", f["Sentence Audio"].strip())):
            if not val:
                bad.append((word, "EMPTY", f"{label} is empty"))
        for field in ("Sentence Audio", "Definition Audio"):
            for fn in re.findall(r"\[audio:(.+?)\]", f.get(field, "")):
                if fn not in present:
                    bad.append((word, "MEDIA", f"{field} → {fn} not in collection.media"))
                elif os.path.getsize(os.path.join(MEDIA, fn)) < MIN_MP3:
                    bad.append((word, "MEDIA", f"{field} → {fn} is a stub"))
        d = f.get("Definition", "")
        if d and d.count('def-type="bilingual"') != 1:
            bad.append((word, "DEF", "not exactly one bilingual block"))
        if 'def-type="monolingual"' in d:
            bad.append((word, "DEF", "a monolingual block was made"))
    return bad


def review(notes):
    """Opus reads word + sentence + definition together. Returns [(word, category, why)]."""
    items = [(100000 + i, n) for i, n in enumerate(notes)]
    verdicts, pending = {}, items
    for attempt in range(3):
        if not pending:
            break
        found = {}
        for start in range(0, len(pending), GROUP):
            chunk = pending[start:start + GROUP]
            body = "\n".join(
                "{}: WORD {} | SENTENCE {}{}".format(
                    i, n["fields"]["Word"]["value"].strip(),
                    strip_html(n["fields"]["Sentence"]["value"]),
                    (" | DEFINITION " + d) if (d := strip_html(
                        n["fields"]["Definition"]["value"])) else "")
                for i, n in chunk)
            text = (f"{len(chunk)} cards. Answer with exactly {len(chunk)} lines, one per id, "
                    f"ids echoed exactly as given.\n\n{body}")
            reply, usage, cost = ask(text, SYSTEM, MODEL, what="audit", effort=EFFORT)
            got = {int(m.group(1)): (m.group(2), m.group(3).strip())
                   for m in LINE.finditer(reply)}
            ok = {i: v for i, v in got.items() if i in {j for j, _ in chunk}}
            found.update(ok)
            note(f"    {len(ok)}/{len(chunk)} answered  [{describe(usage)}]  ${cost:.3f}")
        verdicts.update(found)
        pending = [it for it in pending if it[0] not in verdicts]
        if pending:
            note(f"    re-asking {len(pending)} that did not come back")
    out = []
    for i, n in items:
        cat, why = verdicts.get(i, ("OK", ""))
        if cat in CATEGORIES:
            out.append((n["fields"]["Word"]["value"].strip(), cat, why, n["noteId"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="suspend the flagged cards and tag them sm-audit-<category>")
    a = ap.parse_args()

    cfg = load_config()
    notes = load_notes(cfg)
    note(f"{len(notes)} notes in {deck_main(cfg)}")

    mech = mechanical(notes)
    note(f"  mechanical: {len(mech)} problem(s)")
    for w, cat, why in mech[:20]:
        note(f"    {w}: {cat} — {why}")

    findings = [] if a.no_llm else review(notes)
    from collections import Counter
    note(f"\n  review: {len(findings)} flagged of {len(notes)} "
         f"({', '.join(f'{k}={v}' for k, v in Counter(f[1] for f in findings).most_common())})")

    # --no-llm writes its own file. It must never overwrite a real review: doing that once
    # replaced 40 findings with an empty file, and the --apply in the same command then had
    # nothing to act on.
    path = work_dir(cfg) / ("audit-mechanical.tsv" if a.no_llm else "audit.tsv")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("word\tcategory\tnote\tnote_id\n")
        for w, cat, why, nid in findings:
            fh.write(f"{w}\t{cat}\t{why}\t{nid}\n")
        for w, cat, why in mech:
            fh.write(f"{w}\t{cat}\t{why}\t\n")
    note(f"  wrote {path}")

    if a.apply:
        # Read from audit.tsv rather than this run's findings, so `--apply` can be run on its
        # own after a review, and so `--no-llm --apply` still applies the last real review.
        import csv
        saved = work_dir(cfg) / "audit.tsv"
        if not saved.exists():
            sys.exit(f"no {saved} to apply — run the review first")
        by_cat = {}
        with open(saved, encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                if r.get("note_id"):
                    by_cat.setdefault(r["category"], []).append(int(r["note_id"]))
        for cat, nids in by_cat.items():
            anki_request("addTags", notes=nids, tags=f"sm-audit-{cat.lower()}")
        cards = anki_request("findCards", query="tag:sm-audit-*")
        anki_request("suspend", cards=cards)
        note(f"  suspended and tagged {len(cards)} card(s) — "
             f"unsuspend with: tag:sm-audit-* → Cards ▸ Toggle Suspend")


if __name__ == "__main__":
    main()
