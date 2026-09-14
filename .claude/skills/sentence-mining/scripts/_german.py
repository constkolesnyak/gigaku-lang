"""The German half: which interpreter runs spaCy, what "known" means, and the frequency gate.

Three facts shape this module, and each of them cost a measurement (2026-09-04, one
21-minute video of one playlist, 252 sentences, 369 distinct unknown lemmas):

1. **Known is Migaku, and only Migaku** — the user's own rule. Not the gigaku word cache:
   `Word` carries no provenance field, so 345 of its 9,798 German words (AnkiMorphs
   assertions, LR leftovers) cannot be told apart from Migaku's, and `gigaku words`
   additionally rewrites the cache as a side effect of being read. `migaku/de.csv` from
   the daily backup is the only attributable source. See references/known-words.md.

2. **Migaku's `dictForm` is not a lemma** (`piratenköniginnen`, `brukterern`, `anschaust`),
   while spaCy's is (`Piratenköniginn`, `Brukterer`). Neither side can be normalised onto
   the other, so a token is matched BOTH ways: known if its surface is in the set or if
   its lemma is, against a set that is itself the forms plus their lemmas. That widening
   turned 9,456 Migaku forms into 10,116 matchable ones.

3. **Absent from Migaku ≠ unknown.** The i+1 list is headed by `eigentlich` (#1 in spoken
   YouTube German), `bekommen` (#9), `sogar` (#13), `geil` (#14) — 114 of the 369 sit in
   the top 1500 of `freq/de-yt.tsv`. They are bookkeeping gaps, not vocabulary gaps.
   `bucket()` routes them to a grading list instead of to cards, and they count as known
   while computing an i-level (counting them as unknown makes every sentence i+3 and
   destroys the signal). Nothing is swallowed: every one is written out to be graded.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from glob import glob

REEXEC_GUARD = "SM_SPACY_REEXEC"


def spacy_python():
    """The AnkiMorphs spaCy venv's python — the same model AnkiMorphs recalc matches against.

    Version-sorted numerically ("3_13" < "3_9" as strings, so a lexicographic pick hands a
    stale 3.9 venv the job the day a new one appears), and a hit must actually run: the glob
    happily returns a venv whose interpreter is a dangling symlink, which is exactly what an
    Anki python bump leaves behind. Verbatim in behaviour from lib/vocab/words.py:_spacy_python.
    """
    def version(path):
        return [int(n) for n in re.findall(r"\d+", path.rsplit("python-", 1)[-1])]

    hits = [p for p in glob(os.path.expanduser(
        "~/Library/Application Support/Anki2/addons21/spacy-venv-python-*/bin/python"))
        if os.path.exists(p)]  # exists() follows symlinks; a dangling one only lexists
    return max(hits, key=version) if hits else None


def ensure_spacy():
    """Re-exec this script under the spaCy venv if the current interpreter has no spaCy.

    Sentence analysis needs POS tags in context, not just isolated-word lemmas, so shuttling
    a word list through a subprocess (the `_de_lemmas` pattern) is not enough — the whole
    script has to run where spaCy lives. Doing it here means no caller ever has to name the
    interpreter, and `python3 analyze.py` just works.
    """
    try:
        import spacy  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get(REEXEC_GUARD):
        sys.exit("spaCy is not importable even under the venv interpreter — "
                 "run `python3 scripts/check.py --probe`.")
    py = spacy_python()
    if not py:
        sys.exit("No AnkiMorphs spaCy venv found under Anki2/addons21/spacy-venv-python-*.\n"
                 "It dies on Anki python bumps; reinstall AnkiMorphs' spaCy to rebuild it.")
    os.environ[REEXEC_GUARD] = "1"
    os.execv(py, [py, os.path.abspath(sys.argv[0])] + sys.argv[1:])


def load_nlp(cfg):
    import spacy
    # parser/ner are dead weight here: we need POS + lemma, nothing structural.
    return spacy.load(cfg["spacy"]["model"], disable=["parser", "ner"])


def migaku_known(cfg, note=print):
    """The lowercase KNOWN dictForms from the daily Migaku backup.

    Freshness is checked and reported rather than enforced: a slightly stale set only means
    a handful of freshly-marked words get offered again, which curation catches, while a
    hard failure here would block a whole run over a backup that did not fire last night.
    """
    path = cfg["known_words"]["migaku_csv"]
    if not os.path.exists(path):
        sys.exit(f"Migaku backup not found at {path} — run `gigaku backup` first.")
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    limit = cfg["known_words"]["max_age_days"]
    forms = set()
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("knownStatus") == "KNOWN" and r.get("del") == "0":
                w = (r.get("dictForm") or "").strip().lower()
                if w:
                    forms.add(w)
    note(f"  migaku KNOWN: {len(forms)} forms ({age_days:.1f}d old)")
    if age_days > limit:
        note(f"  WARNING: that backup is older than {limit}d — `gigaku backup` may be stuck.")
    return forms


def known_set(cfg, nlp, note=print):
    """Migaku's forms ∪ their spaCy lemmas — the set a token is matched against both ways.

    Warm-started from gigaku's own lemma cache (~10k entries it already paid for), so a cold
    run only lemmatises what the cache has never seen.
    """
    forms = migaku_known(cfg, note=note)
    cache_path = cfg["spacy"]["lemma_cache"]
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}  # a corrupt cache is a slow run, not a failed one
    known = set(forms)
    for w in forms:
        hit = cache.get(w)
        if hit:
            known.add(hit.lower())
    missing = [w for w in forms if w not in cache]
    if missing:
        for w, doc in zip(missing, nlp.pipe(missing, batch_size=512)):
            toks = [t for t in doc if not t.is_space]
            if len(toks) == 1:
                known.add(toks[0].lemma_.lower())
    note(f"  known ∪ lemma(known): {len(known)} forms "
         f"({len(forms)} from Migaku, {len(missing)} lemmatised fresh)")
    return known


def cache_known(cfg, note=print):
    """German words gigaku's own cache holds that Migaku does not — the 345-word gap.

    The cache cannot be filtered by source, so it is useless as *the* known set (see
    references/known-words.md). It is still a witness: a word LR or AnkiMorphs recorded but
    Migaku never got is far more likely to be an unmarked word than a new one — measured,
    `danke` and `junge` are in it. Such a word is routed to the grading list, never silently
    treated as known and never taught. That is the standing rule, not a frequency floor.
    """
    path = cfg["known_words"].get("gigaku_cache") or ""
    if not path or not os.path.exists(path):
        return set()
    try:
        rows = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    out = {r["word"].lower() for r in rows if r.get("language") == "de" and r.get("word")}
    note(f"  gigaku cache (second witness): {len(out)} German words")
    return out


def frequency(cfg, note=print):
    """(rank, meta, membership) over the repo's own German frequency lists.

    `de-yt.tsv` is rank-ordered over the spoken German of the channels the user actually
    watches — that ordering is what the grading gate reads. Its own `migaku` column is a
    SNAPSHOT and is deliberately ignored: grading status is recomputed live from de.csv.
    `de-words.tsv` is not rank-ordered, so it contributes membership only ("is this a real
    German word at all"), which is what separates `reinziehen` from `Punchman`.
    """
    rank, meta = {}, {}
    with open(cfg["frequency"]["yt_tsv"], encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            w = r["word"].strip().lower()
            if w and w not in rank:
                rank[w] = int(r["rank"])
                meta[w] = (r.get("pm", ""), r.get("ru", ""))
    membership = set(rank)
    with open(cfg["frequency"]["words_tsv"], encoding="utf-8") as f:
        for line in f:
            w = line.split("\t")[0].strip().lower()
            if w:
                membership.add(w)
    note(f"  frequency: de-yt {len(rank)} ranked, membership {len(membership)}")
    return rank, meta, membership


CONTENT_POS = {"NOUN", "VERB", "ADJ", "ADV", "PROPN"}

KNOWN, GRADE, CARD, ODD = "known", "grade", "card", "odd"


def bucket(lemma, surface, known, rank, membership, gate, seen=frozenset()):
    """Which of the four fates a token meets. See the module docstring for why there are four.

    ODD is not garbage — it is the residue the lists cannot judge. It holds English and anime
    nouns (Punchman, Isekai), ASR corpses (Messlatt, hochwoten) AND real German the lists
    simply lack (hochvoten, vollgeballern, wegdiskutieren). Only a reader with the sentence in
    front of them can tell those apart, so ODD is carried forward for curation, never dropped
    here.
    """
    lo, so = lemma.lower(), surface.lower()
    if lo in known or so in known:
        return KNOWN
    r = rank.get(lo, rank.get(so))
    if r is not None and r <= gate:
        return GRADE
    if lo in seen or so in seen:
        return GRADE          # a second witness knows it; grade it rather than teach it
    if r is not None or lo in membership or so in membership:
        return CARD
    return ODD


def sort_key(c):
    """Most useful first — the one order both analyze.py and triage.py sort by.

    A ranked word sorts by its rank in spoken YouTube German. An unlisted word has no rank
    by definition, so it sorts after every ranked word and, among its own kind, by how often
    the playlist actually used it — the only usefulness signal such a word has.
    """
    if c.get("yt_rank"):
        return (0, c["yt_rank"], c["unknown_count"], c["stem"], c["sentence_idx"])
    return (1, -c.get("hits", 0), c["unknown_count"], c["stem"], c["sentence_idx"])
