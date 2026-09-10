"""The one fold: surface form + in-context POS → lemma. Shared by fold7 and blend.

This used to live twice — fold7.py and blend.py each carried a copy of resolve() with a
comment promising they were identical — and the 2026-08-24 audit is why it now lives once:
every defect class found in the shipped list traced to the fold, so a fix applied to one
copy and not the other would have produced two silently different lists.

What the audit measured, and what each table/rule here answers:

  * `de_lemmas.tsv` was polluted with inflected-form pages — `brachte`, `laßt`, `wüßte`
    all carried `verb` headword rows, because German Wiktionary gives conjugated forms and
    old spellings real entries. That forced fold7's 3x dominant-lemma merge, and the merge
    ignored POS and edge quality: `führen` (a top-250 verb) was swallowed by `fahren`
    through an archaic form_of sense, `hinweisen` by `Hinweis` through the capitalised
    dative plural, `bescheiden` by `beschissen` through a euphemism link. wikt.py now
    classifies senses, so the lemma table holds TRUE lemmas only, edges carry a kind, and
    the merge is gone: a true lemma of the token's POS can never be attributed elsewhere.
  * Old orthography survived as vocabulary (`laßt` #267, `gewußt` #1745…): their pages are
    "headwords" and the ss/ß rule only looked in Migaku. Spelling edges (`old`/`alt`) are
    now followed as a normalisation pass *before* resolution, so `laßt` → `lasst` →
    `lassen` and Swiss `fleissig` folds into `fleißig` instead of ranking twice.
  * `bessere` tagged ADJ landed on the verb `bessern` (rank 14): the pos-strict ladder
    found no adj entry and the any-pos fallback crossed POS. A simplemma hop now sits
    between the two: `bessere` → `besser` → its adj edge → `gut`.
  * Sentence-initial capitalised verbs tagged NOUN minted phantom nouns (`Denke` #569,
    `Schalte` #1150, and part of `Macht`'s rank): the ctx pass now records whether a
    sighting was sentence-initial, and a capitalised NOUN sighting from that position is
    dropped when the lowercase twin conjugates a verb — that position's capital carries no
    case evidence, so those tokens are unattributable and claiming them for the noun is
    how the phantoms were minted. Mid-sentence sightings (`die Macht`) still count.
"""
import os
from collections import defaultdict

import simplemma

D = os.path.dirname(os.path.abspath(__file__))
UPOS2WIKT = {"NOUN": "noun", "VERB": "verb", "AUX": "verb", "ADJ": "adj", "ADV": "adv",
             "PRON": "pron", "DET": "det", "ADP": "prep", "CCONJ": "conj",
             "SCONJ": "conj", "PART": "particle", "INTJ": "intj", "NUM": "num",
             "PROPN": "name"}


def clean(f):
    return not ("'" in f or "’" in f or "." in f) and any(c.isalpha() for c in f)


class Dict:
    """The Wiktionary tables, loaded once.

    head/canon — TRUE lemmas only (wikt.py keeps a page only if some sense of that POS is
    a real sense, not a form_of/alt_of/spelling one). fof — inflection edges, POS-keyed.
    spell — old/variant spelling edges, followed before anything else. minor — a form_of
    sense on a page that is itself a lemma of that POS (führen→fahren): recorded so the
    Migaku filter can refuse to treat the word as "just a form", never followed here.
    wide — lemma pages' declension tables, the last-resort net for forms with no entry.
    """

    def __init__(self, d=D):
        self.head = defaultdict(set)
        self.canon = {}
        self.enreal = set()          # (lower, pos) with real senses in en.wiktionary
        for line in open(f"{d}/de_lemmas.tsv", encoding="utf-8"):
            w, p, *src = line.rstrip("\n").split("\t")
            self.head[w.lower()].add(p)
            self.canon.setdefault((w.lower(), p), w)
            if src and "en" in src[0]:
                self.enreal.add((w.lower(), p))
        self.fof = defaultdict(lambda: defaultdict(set))
        self.fof_any = defaultdict(set)
        self.minor = defaultdict(set)
        self.both = defaultdict(lambda: defaultdict(set))   # lemma AND form of target
        self.degree = defaultdict(set)                      # comparative/superlative of
        self.spell = {}
        for line in open(f"{d}/de_formof.tsv", encoding="utf-8"):
            w, p, t, kind = line.rstrip("\n").split("\t")
            if kind in ("old", "alt"):
                self.spell.setdefault(w, t)
            elif kind == "minor":
                self.minor[w].add(t)
            elif kind == "both":
                self.both[w][p].add(t)
            elif kind == "degree":
                self.degree[w].add(t)
            else:
                self.fof[w][p].add(t)
                self.fof_any[w].add(t)
        self.wide = defaultdict(set)
        for line in open(f"{d}/de_forms.tsv", encoding="utf-8"):
            q = line.rstrip("\n").split("\t")
            if len(q) >= 2:
                self.wide[q[0]].add(q[1])
        # A spelling edge is believed only on a form with no other reading. en-wikt
        # calls `Weißen` an obsolete spelling of `Weizen`, and normalize() follows
        # spelling before anything else — so every `die Weißen` in the corpus was
        # teleported into wheat (297 of Weizen's 339 tokens, rank 100 for a 3/M word).
        # A form that also carries inflection edges or is a lemma itself has a living
        # reading, and a living reading outranks an obsolete-spelling claim in a
        # corpus of modern subtitles. `laßt`, `Weitzen`, `fleissig` have no other
        # reading and keep their edges.
        self.spell = {w: t for w, t in self.spell.items()
                      if not (w in self.fof_any or w in self.both
                              or self.head.get(w.lower()))}

    def normalize(self, form):
        """Follow spelling edges (laßt→lasst, fleissig→fleißig), a few hops, cycle-safe."""
        seen = {form}
        for _ in range(3):
            t = self.spell.get(form) or self.spell.get(form.lower())
            if not t or t in seen:
                return form
            # a lowercase-keyed edge must not decapitalise a noun sighting
            form = t if (self.spell.get(form) or not form[:1].isupper()) else t.capitalize()
            seen.add(form)
        return form

    def verbish(self, lower):
        """Does this lowercase spelling conjugate a verb? (`denke`, `macht` — yes;
        `junge` — no.) Decides whether a sentence-initial capital could be a verb."""
        return "verb" in self.head.get(lower, ()) or bool(self.fof[lower].get("verb"))


def load_forms(path, dic):
    """Read a ctx pass output (form, POS, sentence-initial flag, count).

    Drops the two unattributable classes. A capitalised sighting tagged NOUN or PROPN in
    sentence-initial position whose lowercase twin conjugates a verb: that position's
    capital carries no case evidence, and spaCy reads the verb there as a noun (`Denke
    ich…` minted a phantom noun at rank 569) or a name (`Mach das!` gave the Mach number
    255 per million). And X-tagged tokens outright: that is spaCy's "foreign material"
    verdict, subtitle English mostly, and counting it attached 413 sightings of English
    `my` to the Greek letter `My`. `sfreq` (surface frequency, for tie-breaks) is summed
    over everything regardless.
    """
    total, rows, sfreq, dropped = 0, defaultdict(int), defaultdict(int), 0
    for line in open(path, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if p[0] == "#tokens":
            total = int(p[1])
            continue
        if len(p) != 4 or not clean(p[0]):
            continue
        w, pos, initial, n = p[0], p[1], p[2] == "1", int(p[3])
        sfreq[w.lower()] += n
        if pos == "X":
            continue
        if (initial and pos in ("NOUN", "PROPN") and w[:1].isupper()
                and dic.verbish(w.lower())):
            dropped += n
            continue
        rows[(w, pos)] += n
    return [(w, p, n) for (w, p), n in rows.items()], total, sfreq, dropped


def make_resolver(dic, sfreq):
    def best(opts, want=None, form=None):
        """Pick among the lemmas claiming a form.

        Corrections that live here (all found by reading output): `sfreq` is keyed
        lowercase, so a candidate spelled like the form being resolved gets no credit
        from it — otherwise `tränen` inherits the 306 sightings of `Tränen` and beats
        `Träne` at its own tie-break. A noun's lemma is capitalised, so for a noun the
        capitalised candidate wins outright. And a candidate that is a true lemma
        outranks one that is itself a form page, so the any-POS fallback can no longer
        land on a rare same-POS homograph.
        """
        def score(c):
            s = 0 if (form and c.lower() == form.lower()) else sfreq.get(c.lower(), 0)
            return (1 if (want == "noun" and c[:1].isupper()) else 0,
                    1 if dic.head.get(c.lower()) else 0, s, -len(c))
        return max(opts, key=score)

    def resolve(form, upos):
        form = dic.normalize(form)
        want = UPOS2WIKT.get(upos)
        probe = form if upos in ("NOUN", "PROPN") else form.lower()
        # spaCy tags a predicative adjective ADV (`Das ist steif/hervorragend`) — a
        # fact fold7's filter already honours (`lange`, `weiter`) and this resolver
        # did not: with only `adv` wanted, the strict ladder failed on adj-only
        # lemmas and the any-POS fallback handed the word to a verb through a
        # homograph inflection edge — `hervorragend` fed hervorragen (16/M for a
        # 0.04/M verb), bare `munter`/`steif`/`schräg` fed muntern/steifen/schrägen
        # via their imperatives. An ADV sighting therefore also stands on an adj
        # entry, adv first so true adverb lemmas keep winning.
        wants = (want, "adj") if upos == "ADV" else (want,)

        def strict(f):
            for wa in wants:
                if wa and wa in dic.head.get(f.lower(), ()):
                    return dic.canon.get((f.lower(), wa), f)
                if wa and f in dic.fof and wa in dic.fof[f]:
                    return best(dic.fof[f][wa], wa, f)
            return None

        for f in (probe, form):
            r = strict(f)
            if r:
                return r
        # the simplemma hop: `bessere` has no adj entry and no adj edge, but its
        # dictionary form `besser` does — one lookup away, still POS-strict.
        m = simplemma.lemmatize(probe, lang="de")
        if m and m.lower() != probe.lower():
            for f in (m if upos in ("NOUN", "PROPN") else m.lower(), m):
                r = strict(f)
                if r:
                    return r
        for f in (probe, form):
            if f in dic.fof_any:
                return best(dic.fof_any[f], want, f)
            if dic.head.get(f.lower()):
                # prefer a real word class: alphabetical choice picked `abbrev` over
                # `noun` and minted ZIMT (a dewikt Abkürzung page) as a lemma of its
                # own beside Zimt, fed by emphatic all-caps dialogue
                pos = min(dic.head[f.lower()],
                          key=lambda p: (p in ("abbrev", "symbol", "punct",
                                               "character", "phrase", "name"), p))
                return dic.canon.get((f.lower(), pos), f)
        return m or probe

    return resolve


def fold_corpus(path, dic):
    """One corpus → lemma frequencies. The whole pipeline's core, in one place.

    `own` counts the tokens a lemma received from its OWN spelling: a noun or a bare
    comparative gets ~all of its mass that way, a verb spreads over its conjugation —
    which is what lets the filter tell `besser` (100% self-spelt, gut's comparative)
    from `gefallen` (fed by gefällt/gefiel, a verb in its own right).
    """
    raw, total, sfreq, dropped = load_forms(path, dic)
    resolve = make_resolver(dic, sfreq)
    freq, propn = defaultdict(int), defaultdict(int)
    own, pos_of = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for form, upos, n in raw:
        w = resolve(form, upos)
        if not w or not any(c.isalpha() for c in w):
            continue
        freq[w] += n
        pos_of[w][upos] += n
        if form.lower() == w.lower():
            own[w] += n
        if upos == "PROPN":
            propn[w] += n
    return freq, pos_of, propn, total, sfreq, dropped, own
