#!/usr/bin/env python3
"""Transcripts → card candidates + a grading list, diffed against Migaku alone.

    python3 scripts/analyze.py                     # every episode in the work dir's manifest
    python3 scripts/analyze.py --only p01          # one episode
    python3 scripts/analyze.py --dry               # print the split, write nothing

Re-execs itself under the AnkiMorphs spaCy venv (see `_german.ensure_spacy`), so the caller
never has to name that interpreter.

Writes `candidates.json` (what may become cards, after curation) and `grade-me.tsv` (what
the user should mark in Migaku instead of being taught). The three-way split those two files
represent is the whole point of this script; `_german.bucket` holds the reasoning.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _german import ensure_spacy                                    # noqa: E402
ensure_spacy()                                                       # must precede spaCy users

from _config import load_config, deck_main, deck_deferred, work_dir  # noqa: E402
from _german import (CARD, CONTENT_POS, GRADE, ODD, bucket,          # noqa: E402
                     cache_known, frequency, known_set, load_nlp, sort_key)

# freqdeck owns the separable-prefix list; importing it keeps one copy of a list that was
# written down precisely because no lemmatiser can derive it.
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "freqdeck"))
import deck as freqdeck                                              # noqa: E402

PREFIXES = set(freqdeck.PREFIXES)

# A clip wants to be long enough to carry meaning and short enough to stay a flashcard.
OPTIONS_PER_LEMMA = 8   # every sentence a word was heard in that the clarity pass may choose
GOOD_CHARS = (30, 170)
GOOD_MS = (1800, 13000)
IDEAL_CHARS = 85

GERMAN_WORD = re.compile(r"^[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß-]{2,}$")


def mechanical_drop(lemma, pos, b):
    """The part of the unlisted bucket a rule can judge, so Opus is not paid to.

    Only ODD is filtered — a word the frequency lists already vouch for is never dropped
    here. Measured on episode 1 (201 unlisted): PROPN alone is 54 of them and is essentially
    pure noise — anime titles, character names, English nouns, a stray Korean glyph, a bare
    "L" — because a capitalised German noun the lists know never reaches ODD in the first
    place. The two German verbs it costs (`Checkt`, `Pfeifst`, sentence-initial and so
    mis-tagged PROPN) are lemmatiser failures that would have been dropped downstream anyway.
    """
    if b != ODD:
        return ""
    if not GERMAN_WORD.match(lemma):
        return "not-a-german-word-shape"
    if pos == "PROPN":
        return "proper-noun"
    return ""


def note(msg):
    print(msg, file=sys.stderr, flush=True)


def anki(url, action, **params):
    body = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
    if resp.get("error"):
        raise RuntimeError(f"AnkiConnect: {resp['error']}")
    return resp["result"]


def existing_words(cfg, rebuild=False):
    """Lower-cased `Word` values already carded, so nothing is minted twice.

    Deliberately not the 46,859-card sentence-bank deck: that is reference
    material the user does not study, so a word appearing there is not a duplicate.
    """
    decks = [d for d in cfg["dedupe_decks"] if d]
    if rebuild:
        # A rebuild re-decides every word in the target deck, so its own words must not
        # count as duplicates of themselves. Dropping the exact name is not enough: the
        # target is a SUBDECK (`YouTube 🇩🇪::<channel>`) while dedupe_decks lists the parent,
        # and Anki's `deck:"X"` matches X's subdecks — so the parent quietly re-excluded
        # every word the deck already had. Measured: 479 candidates → 369, and the words
        # carrying the deck's best sentences were exactly the ones removed.
        target = deck_main(cfg)
        decks = [d for d in decks if d != target and not target.startswith(d + "::")]
    if not decks:
        return set()
    q = "(" + " OR ".join(f'deck:"{d}"' for d in decks) + ")"
    ids = anki(cfg["anki_connect_url"], "findNotes", query=q)
    field = cfg["field_map"]["word"]
    out = set()
    for i in range(0, len(ids), 1000):
        for n in anki(cfg["anki_connect_url"], "notesInfo", notes=ids[i:i + 1000]):
            v = n["fields"].get(field, {}).get("value", "").strip().lower()
            if v:
                out.add(v)
    note(f"  already carded in {decks}: {len(out)} words")
    return out


def existing_sentences(cfg):
    """Sentences already on a 🇩🇪 note in the target decks.

    Seeds the claim set so a re-run does not hand a word a sentence an earlier run already
    used — Anki would reject it at push time, silently costing that word its card.
    """
    decks = [d for d in (deck_main(cfg), deck_deferred(cfg)) if d]
    if not decks:
        return set()
    q = "(" + " OR ".join(f'deck:"{d}"' for d in decks) + ")"
    ids = anki(cfg["anki_connect_url"], "findNotes", query=q)
    field = cfg["field_map"]["sentence"]
    out = set()
    for i in range(0, len(ids), 1000):
        for n in anki(cfg["anki_connect_url"], "notesInfo", notes=ids[i:i + 1000]):
            v = n["fields"].get(field, {}).get("value", "").strip()
            if v:
                out.add(v)
    return out


def merge_separables(doc, membership):
    """Reattach a stranded separable prefix to its verb: "zieh … rein" → `reinziehen`.

    Two tests, and both are needed — the first version of this had only the membership test
    and produced a *wrong* card: in "Teamwork aus dem Fokus genommen … auf den Kopf gestellt"
    it glued the preposition `aus` onto `stellen` and minted `ausstellen`, a word that is not
    in the sentence at all. freqdeck's own note says why no lemmatiser saves you here — it
    "cannot tell `an` in \"Mach das Licht an\" from the preposition" — so the tests are
    syntactic, not lexical:

      1. the particle is tagged ADV or PTKVZ, never ADP. A stranded particle has no object;
         `aus` governing "dem Fokus" is tagged ADP and is excluded by this alone.
      2. the particle sits at the end of its clause, which is where German syntax puts a
         stranded particle and where a preposition never is (a preposition is followed by
         its noun phrase).

    Membership in the frequency lists is then the third test, so a merge must also produce a
    word German actually has. A verb that fails any of the three is simply left unmerged,
    which costs a card and never invents one.
    """
    merged, drop = {}, set()
    verbs = [t for t in doc if t.pos_ in ("VERB", "AUX")]
    for t in doc:
        is_particle = t.pos_ in ("ADV", "PART") or t.tag_ == "PTKVZ"
        if t.text.lower() not in PREFIXES or not is_particle:
            continue
        nxt = next((x for x in doc[t.i + 1:] if not x.is_space), None)
        if nxt is not None and not nxt.is_punct:
            continue                      # not clause-final → a preposition, not a particle
        prev = [v for v in verbs if v.i < t.i]
        if not prev:
            continue
        v = prev[-1]
        cand = t.text.lower() + v.lemma_.lower()
        if cand in membership:
            merged[v.i] = cand
            drop.add(t.i)
    return merged, drop


def context_words(doc, target_lemma, target_surface):
    """Content words left once the target is blanked out — the rubric's own test, counted.

    "Blank the target and read what is left" is how clarity is judged; when nothing is left,
    the sentence cannot teach and no score should be able to say otherwise. It did: the rubric
    rated `Also er ist quasi so ein so ein Gauner.` at 66 and `Das ist so ein Maskottchen
    Charakter.` at 60, both comfortably over the floor, because within a word's block it is
    scoring relative to that word's other options.
    """
    t = {target_lemma.lower(), target_surface.lower()}
    return sum(1 for x in doc
               if x.pos_ in ("NOUN", "VERB", "ADJ", "ADV") and x.is_alpha
               and x.lemma_.lower() not in t and x.text.lower() not in t)


def analyse_sentence(doc, known, rank, membership, gate, seen):
    """(unknown[], graded[]) for one sentence — the buckets of `_german.bucket`."""
    merged, drop = merge_separables(doc, membership)
    unknown, graded = [], []
    for t in doc:
        if t.i in drop or not t.is_alpha or t.pos_ not in CONTENT_POS:
            continue
        lemma = merged.get(t.i, t.lemma_)
        b = bucket(lemma, t.text, known, rank, membership, gate, seen)
        if b == GRADE:
            graded.append({"lemma": lemma, "surface": t.text})
        elif b in (CARD, ODD):
            unknown.append({"lemma": lemma, "surface": t.text, "pos": t.pos_, "bucket": b,
                            "start_ms": None})
    return unknown, graded


MAX_SPAN_CHARS = 230
MAX_SPAN_MS = 16000   # upper bound for COLLECTING spans; the selection cuts later (clarity --max-clip)


def expand(sents):
    """Each sentence, plus the same sentence with a neighbour glued on.

    The transcript is split on punctuation, and YouTube's punctuation cuts clauses short:
    "Das macht man zurecht." on its own says nothing, while the sentence before it says what
    "das" was. Measured on this playlist, half the words occur exactly once — so without this
    there is nothing for the clarity pass to choose between, and a word's only sentence is
    whatever fragment it happened to land in.

    The neighbours are contiguous in the audio, so a span clips as one continuous cut with no
    splice. Spans are capped by length and duration: past ~16 s a card stops being a card.
    """
    out = []
    for i, s in enumerate(sents):
        for lo, hi in ((i, i), (i - 1, i), (i, i + 1), (i - 1, i + 1)):
            if lo < 0 or hi >= len(sents):
                continue
            span = sents[lo:hi + 1]
            text = " ".join(x["text"] for x in span)
            start, end = span[0]["start_ms"], span[-1]["end_ms"]
            if len(text) > MAX_SPAN_CHARS or end - start > MAX_SPAN_MS:
                continue
            words = [w for x in span for w in x["words"]]
            out.append({"idx": i, "text": text, "start_ms": start, "end_ms": end,
                        "words": words, "span": hi - lo + 1})
    return out


MAX_PAD_MS = 220     # the most air taken along when the pause is long
MIN_PAD_MS = 40      # and the least, when the neighbouring word is right up against it


def clip_bounds(sent, words):
    """Clip bounds placed in the PAUSE between words, not on the phrase's timing.

    A clip used to be [start-180ms, end+180ms] with a constant margin, and that margin
    reached into the tail of the previous word and the start of the next — the phrase began
    and ended in the middle of someone else's word. Now the edge sits in the middle of the
    pause: as much of it as there is, but no more than MAX_PAD_MS (or extra silence gets into
    the clip) and no less than MIN_PAD_MS (or the first sound's own attack is cut off).
    """
    lo, hi = sent["start_ms"], sent["end_ms"]
    prev_end = max((w["end_ms"] for w in words if w["end_ms"] <= lo), default=None)
    next_start = min((w["start_ms"] for w in words if w["start_ms"] >= hi), default=None)
    gap_before = (lo - prev_end) if prev_end is not None else MAX_PAD_MS
    gap_after = (next_start - hi) if next_start is not None else MAX_PAD_MS
    pad_before = min(MAX_PAD_MS, max(MIN_PAD_MS, gap_before // 2))
    pad_after = min(MAX_PAD_MS, max(MIN_PAD_MS, gap_after // 2))
    return max(0, lo - pad_before), hi + pad_after


def word_start(sent, surface):
    """The target word's own start time — for the source link, and for a word-level clip."""
    for w in sent["words"]:
        if surface in w["text"]:
            return w["start_ms"]
    return sent["start_ms"]


def sentence_penalty(sent):
    """Lower is better. Separates two sentences that tie on unknown count."""
    n, dur = len(sent["text"]), sent["end_ms"] - sent["start_ms"]
    off = not (GOOD_CHARS[0] <= n <= GOOD_CHARS[1] and GOOD_MS[0] <= dur <= GOOD_MS[1])
    return (int(off), abs(n - IDEAL_CHARS))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated stems, e.g. p01,p03")
    ap.add_argument("--dry", action="store_true", help="print the split; write nothing")
    ap.add_argument("--gate", type=int, default=None, help="override frequency.grade_gate")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-decide every word, ignoring what the target deck already has")
    a = ap.parse_args()

    cfg = load_config()
    wd = work_dir(cfg)
    gate = a.gate if a.gate is not None else cfg["frequency"]["grade_gate"]
    only = {s.strip() for s in a.only.split(",") if s.strip()}

    manifest = json.loads((wd / "manifest.json").read_text(encoding="utf-8"))
    if only:
        manifest = [m for m in manifest if m["stem"] in only or m["stem"].split("-")[0] in only]
    if not manifest:
        sys.exit("nothing to analyse — run fetch.py first")

    note(f"loading spaCy + the known set (gate {gate})…")
    nlp = load_nlp(cfg)
    known = known_set(cfg, nlp, note=note)
    rank, meta, membership = frequency(cfg, note=note)
    seen = cache_known(cfg, note=note) - known
    existing = set() if a.dry else existing_words(cfg, a.rebuild)

    grade_rows, counts = {}, {"sentences": 0, "i0": 0, "i1": 0, "i2": 0, "i3plus": 0}
    hits, options, all_words = Counter(), {}, {}
    for m in manifest:
        t = json.loads(open(m["transcript"], encoding="utf-8").read())
        all_words[m["stem"]] = t["words"]
        sents = expand(t["sentences"])
        for sent, doc in zip(sents, nlp.pipe([s["text"] for s in sents], batch_size=128)):
            unknown, graded = analyse_sentence(doc, known, rank, membership, gate, seen)
            if sent["span"] == 1:
                counts["sentences"] += 1
                counts[{0: "i0", 1: "i1", 2: "i2"}.get(len(unknown), "i3plus")] += 1
            for g in graded:
                grade_rows.setdefault(g["lemma"].lower(),
                                      {"word": g["lemma"], "example": sent["text"], "n": 0})["n"] += 1
            for u in unknown:
                key = u["lemma"].lower()
                hits[key] += 1
                score = (len(unknown), *sentence_penalty(sent), sent["span"], sent["idx"])
                # Keep several options per lemma, not just the winner: 🇩🇪 German's FIRST field
                # is `Sentence`, so Anki's duplicate check fires on the sentence, and two words
                # whose best sentence is the same one cannot both become cards. Measured: 56
                # candidates collided across 27 sentences, and the whole addNotes batch aborted.
                opts = options.setdefault(key, [])
                opts.append((score, dict(sent, unknown=unknown,
                                         context=context_words(doc, u["lemma"], u["surface"])),
                             m, u))
                opts.sort(key=lambda o: o[0])
                del opts[OPTIONS_PER_LEMMA:]
    dupes = [k for k in options if k in existing]
    for k in dupes:
        del options[k]

    def build(cfg, k, opt):
        _, sent, m, u = opt
        n = len(sent["unknown"])
        clip = clip_bounds(sent, all_words[m["stem"]])
        return {"lemma": u["lemma"], "surface": u["surface"], "pos": u["pos"],
                "bucket": u["bucket"], "sentence": sent["text"], "stem": m["stem"],
                "audio": m["audio"], "url": m["url"], "title": m["title"],
                "sentence_idx": sent["idx"], "sentence_start_ms": sent["start_ms"],
                "sentence_end_ms": sent["end_ms"],
                "clip_start_ms": clip[0], "clip_end_ms": clip[1],
                "target_word_start_ms": word_start(sent, u["surface"]),
                "unknown_count": n, "span": sent.get("span", 1),
                "context": sent.get("context", 0), "i_level": f"i{min(n, 9)}",
                "deck": deck_main(cfg) if n == 1 else deck_deferred(cfg),
                "yt_rank": rank.get(k), "hits": hits[k], "ru": meta.get(k, ("", ""))[1]}

    provisional, mech = [], []
    for k, opts in options.items():
        c = build(cfg, k, opts[0])
        why = mechanical_drop(c["lemma"], c["pos"], c["bucket"])
        if why:
            c["dropped"] = why
            mech.append(c)
            continue
        provisional.append((k, c))
    provisional.sort(key=lambda kc: sort_key(kc[1]))

    # Give every word a sentence of its own. Anki dedupes 🇩🇪 notes on `Sentence` (its first
    # field), so the most useful word claims the shared sentence and the next one falls back
    # to its runner-up rather than being rejected at push time.
    claimed, cands, collided = set(existing_sentences(cfg)), [], []
    for k, prov in provisional:
        pick = next((o for o in options[k] if o[1]["text"] not in claimed), None)
        if pick is None:
            prov["dropped"] = "every sentence it appears in is taken by a better word"
            collided.append(prov)
            continue
        c = build(cfg, k, pick)
        # Every sentence this word was heard in, best-first, so the clarity pass can choose
        # on what the sentence actually teaches rather than on how few unknowns it has.
        # Fewest-unknowns is a cheap proxy and it picked fluent, empty sentences — the most
        # common failure this corpus has.
        c["options"] = [build(cfg, k, o) for o in options[k]]
        claimed.add(c["sentence"])
        cands.append(c)
    cands.sort(key=sort_key)
    capped = cands

    n_card = sum(1 for c in capped if c["bucket"] == CARD)
    tally = Counter(c["dropped"] for c in mech)
    note("  mechanically dropped: %d (%s)"
         % (len(mech), ", ".join(f"{k}={v}" for k, v in tally.most_common()) or "none"))
    note(f"\n  sentences {counts['sentences']}  "
         f"i+0 {counts['i0']}  i+1 {counts['i1']}  i+2 {counts['i2']}  i+3plus {counts['i3plus']}")
    note(f"  grade-me (top-{gate} of de-yt, NOT carded): {len(grade_rows)}")
    note(f"  candidates: {len(cands)} ({n_card} in the frequency lists, "
         f"{len(cands) - n_card} unlisted → triage.py judges those)")
    note(f"  duplicates already carded, dropped: {len(dupes)}")
    note(f"  lost to a sentence collision: {len(collided)}")

    if a.dry:
        note("\n  --dry: nothing written")
        return

    out = {"source": "youtube", "gate": gate, "stats": counts,
           "generated_from": [m["stem"] for m in manifest],
           "candidates": capped, "mechanically_dropped": mech}
    (wd / "candidates.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = sorted(grade_rows.values(), key=lambda r: rank.get(r["word"].lower(), 10 ** 9))
    with open(wd / "grade-me.tsv", "w", encoding="utf-8") as f:
        f.write("word\tyt_rank\tper_million\tru\thits\texample\n")
        for r in rows:
            k = r["word"].lower()
            pm, ru = meta.get(k, ("", ""))
            f.write(f"{r['word']}\t{rank.get(k, '')}\t{pm}\t{ru}\t{r['n']}\t{r['example']}\n")
    note(f"\n  wrote {wd / 'candidates.json'}\n  wrote {wd / 'grade-me.tsv'}")


if __name__ == "__main__":
    main()
