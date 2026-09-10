"""Both Wiktionary editions → the three dictionary tables, with senses CLASSIFIED.

The first extraction took "has a page" as "is a lemma", and that one decision produced
most of the shipped list's defects (audited 2026-08-24): German Wiktionary gives real
pages to conjugated forms (`brachte`), declined forms (`bessere`) and pre-1996 spellings
(`laßt`, `wüßte`, `Anlaß`), so the lemma table certified them all as vocabulary, and the
3x dominant-lemma merge bolted on to compensate then swallowed real words through junk
edges (`führen`→`fahren` via an archaic sense, `bescheiden`→`beschissen` via a euphemism).

What a page IS is written in its senses, so this pass reads them — and the first rebuild
taught four more lessons, each visible in its head row before the fix:

  * a stray "real" sense on a page filed as a form does not make it a lemma: dewikt's
    `gedacht` carries a glossless sense beside its form_of, en's `Freunde` a spurious
    "friends" — the page-level form-of tag and en's `… form` head template outvote them
    (`Freunde` was #5 of the rebuilt list before this rule);
  * en.wiktionary keeps SEPARATE lemma entries for German plurals (`Tage` "that time of
    the month", `Nachrichten` "news") beside the form entries. A (word, pos) that any
    edition files as a form page is a lemma only if de.wiktionary gives it a real entry
    of its own — de knows its plurals, en's are cruft (`Tage` sat at #9);
  * `alt_of` on a TRUE lemma's page is a cross-reference, not a spelling claim: en marks
    `Gesetz` "short for Gesetzessammlung" and `Rücken` "short for Rückenschmerzen", and
    treating those as spelling edges both misrouted the tokens and — worse — deleted
    `Gesetz` and `Rücken` from the lemma table via the variant veto, which is how a
    13.7M-token corpus came to contain `Gesetzsammlung` at 73 per million;
  * an entry whose senses are all form/variant senses is a FORM PAGE — edges, never a
    lemma; a lemma's own form senses become `minor` edges: recorded, never followed —
    `führen` has six real verb senses, and no archaic cross-reference may outvote them.

Two editions because each knows what the other doesn't: en.wiktionary carries clean
machine tags (`alt-of, archaic` on `laßt`, `Switzerland` on `fleissig`) but hand-writes
some German form pages with no structure (`brachte` is "preterite of bringen" in prose,
head template `verb form` — parsed here from the gloss); de.wiktionary has the fuller
declension coverage (`bessere`→`gut` exists only there) and the colloquial lemmas
(`Tusse`, `Angsthase`), and is the authority on which German "plural" is its own word
(`Eltern` yes, `Tage` no).

Outputs: de_lemmas.tsv (word, pos — true lemmas only), de_formof.tsv (word, pos, target,
kind ∈ infl|old|alt|minor; pos `x` = unknown), de_forms.tsv (form, lemma, pos — lemma
pages' inflection tables, the last-resort net).
"""
import gzip
import json
import os
import re
import sys
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
OLD_TAGS = {"obsolete", "superseded", "archaic", "dated"}
# en.wiktionary writes DERIVATION as form_of too — {{female equivalent of}} filed
# `Anwältin` under Anwalt, {{agent noun of}} filed `Tänzer` under tanzen, {{diminutive
# of}} filed `Stückchen` under Stück — and following those edges cut real separate
# vocabulary as "declined forms". The user's standing rule: inflection folds,
# derivation stands. A gender tag alone is not the marker (declined adjective senses
# carry `feminine, nominative, …`): derivation senses carry NO case/degree tag.
DERIV_TAGS = {"diminutive", "augmentative", "agent", "endearing"}
CASE_TAGS = {"nominative", "accusative", "dative", "genitive", "singular", "plural",
             "comparative", "superlative", "predicative", "mixed", "strong", "weak"}
GOOD_POS = {"noun", "verb", "adj", "adv", "intj", "num", "pron", "det", "prep", "conj",
            "particle", "postp", "article", "name", "phrase", "prefix", "suffix",
            "interfix", "circumfix", "punct", "symbol", "character", "abbrev", "proverb",
            "contraction"}
TARGET_RE = re.compile(r"^[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]*$")
# en.wiktionary hand-written form pages: "brought; preterite of bringen, bring."
GLOSS_OF = re.compile(r"\b(?:of|von)\s+([A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]*)")

real_en, real_de = set(), set()    # (word, pos) with a real sense, per edition
formpage = set()                   # (word, pos) filed as a form page by either edition
infl = defaultdict(set)            # (word, pos) -> {target}
minor = defaultdict(set)           # (word, pos) -> {target}
degree = defaultdict(set)          # (word, pos) -> {target}  comparative/superlative of
spell = defaultdict(dict)          # word -> {target: kind}  kind in old|alt
formtabs = defaultdict(set)        # (word, pos) -> {inflected form}  (lemma pages only)


def tgt(ref):
    """`alt_of` targets arrive with prose attached ("lasst which was deprecated in the
    spelling reform") — the target is the first word, and only if it looks like one."""
    w = (ref.get("word") or "").split()[0].strip(",;.") if ref.get("word") else ""
    return w if TARGET_RE.fullmatch(w) else ""


def formish(s):
    return bool(s.get("form_of") or s.get("alt_of")
                or {"form-of", "alt-of"} & set(s.get("tags", ())))


def derivish(s, pos):
    """A form_of sense that is really DERIVATION — a word of its own that happens to be
    written as a cross-reference: female equivalents, agent nouns, diminutives. A gender
    tag alone is not the marker (declined-form senses carry `feminine, nominative, …`):
    a derivation sense carries no case or degree tag."""
    stags = set(s.get("tags", ()))
    return bool(DERIV_TAGS & stags) or bool(
        {"feminine", "masculine"} & stags and not CASE_TAGS & stags and pos == "noun")


def eat(e, edition):
    w, pos = e.get("word"), e.get("pos") or "x"
    if not w or " " in w:
        return
    senses = e.get("senses") or []
    etags = set(e.get("tags") or ())
    real = [s for s in senses if not formish(s)]
    formsenses = [s for s in senses if formish(s) and not derivish(s, pos)]
    deriv = [s for s in senses if formish(s) and derivish(s, pos)]
    hand_form = edition == "en" and any(
        "form" in str((h.get("args") or {}).get("2", ""))
        for h in e.get("head_templates") or ())
    # A derivation page (`Anwältin` = "female equivalent of Anwalt", `Stückchen` =
    # "diminutive of Stück") DEFINES its word — it counts as a real entry, and its
    # cross-reference becomes a minor edge, never a followable form edge.
    page_is_form = (bool({"form-of", "alt-of"} & etags) or hand_form
                    or (bool(formsenses) and not real and not deriv))
    if page_is_form:
        if hand_form and not formsenses:
            # prose-only form page (`brachte`): the target hides in the gloss
            for s in senses:
                m = GLOSS_OF.search((s.get("glosses") or [""])[0])
                if m and TARGET_RE.fullmatch(m.group(1)) and m.group(1) != w:
                    infl[(w, pos)].add(m.group(1))
        real = []
        deriv = []
        formpage.add((w, pos))

    for s in deriv:
        for ref in list(s.get("form_of") or ()) + list(s.get("alt_of") or ()):
            t = tgt(ref)
            if t and t != w:
                minor[(w, pos)].add(t)

    for s in formsenses:
        old = bool(OLD_TAGS & (set(s.get("tags", ())) | etags))
        for ref in s.get("alt_of") or ():
            t = tgt(ref)
            if not t or t == w:
                continue
            if page_is_form:
                spell[w].setdefault(t, "old" if old else "alt")
            else:
                minor[(w, pos)].add(t)
        for ref in s.get("form_of") or ():
            t = tgt(ref)
            if not t or t == w:
                continue
            if page_is_form:
                # a Konjugierte/Deklinierte Form page tagged pos `unknown` still names
                # its kind in the German pos title
                p = pos if pos in GOOD_POS else (
                    "verb" if "Konjugierte" in (e.get("pos_title") or "") else "x")
                infl[(w, p)].add(t)
                # degree pages carry the one semantic marker the fold needs later:
                # `schlimmer` is schlimm's COMPARATIVE (a Komparativ page / en's
                # `comparative` tag) while `lange` is only lang's declined form — the
                # first is the base word's own inflection, the second a real adverb.
                if ("omparativ" in (e.get("pos_title") or "")
                        or "uperlativ" in (e.get("pos_title") or "")
                        or {"comparative", "superlative"} & set(s.get("tags", ()))):
                    degree[(w, p)].add(t)
            else:
                minor[(w, pos)].add(t)

    if (real or deriv) and pos in GOOD_POS:
        (real_en if edition == "en" else real_de).add((w, pos))
        for f in e.get("forms") or ():
            fw = f.get("form") or ""
            if fw and fw != w and " " not in fw and TARGET_RE.fullmatch(fw):
                formtabs[(w, pos)].add(fw)


def stream(path, filt=None):
    n = 0
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if filt and e.get("lang_code") != filt:
                continue
            n += 1
            yield e
    print(f"  {path.split('/')[-1]}: {n:,} entries", file=sys.stderr)


for e in stream(f"{D}/enwikt-german.jsonl.gz"):
    eat(e, "en")
for e in stream(f"{D}/dewikt-raw.jsonl.gz", filt="de"):
    eat(e, "de")

# Lemma status: de real, or en real UNCONTESTED — an (word, pos) either edition files as
# a form page needs de.wiktionary's own real entry to stand (`Tage`/`Nachrichten` fall,
# `Eltern`/`gefallen`/`weiter` stand).
lemmas = {wp for wp in real_de | real_en
          if wp in real_de or wp not in formpage}
# A spelling edge is believed only on a word with NO real lemma status: en gives `Weizen`
# and `Kugel` second entries reading "short for Weizenbier / variant of Kobel", and
# believing those deleted both words from the lemma table and sent Kugel's 554 sightings
# (bullets!) into `Kobel` (a squirrel's nest). `laßt` and `Anlaß` have no real senses
# anywhere, so the reform-spelling edges this exists for all survive.
lemma_words = {w for w, p in lemmas}
spell = {w: ts for w, ts in spell.items() if w not in lemma_words}

head = defaultdict(set)
for w, p in lemmas:
    head[w.lower()].add(p)
out_edges = []
for (w, p), ts in sorted(infl.items()):
    # an inflection edge on a word that IS a true lemma of that pos is not followed —
    # the führen→fahren fix — but it is kept as `both`: the word is simultaneously a
    # lemma and the dictionary's form of something, and the filter wants to know that
    # (`Gedanken` is dewikt's variant lemma AND the plural of Migaku's `Gedanke`).
    kind = "both" if (p != "x" and p in head.get(w.lower(), ())) else "infl"
    for t in sorted(ts):
        out_edges.append((w, p, t, kind))
for (w, p), ts in sorted(degree.items()):
    for t in sorted(ts):
        out_edges.append((w, p, t, "degree"))
for (w, p), ts in sorted(minor.items()):
    for t in sorted(ts):
        out_edges.append((w, p, t, "minor"))
for w, ts in sorted(spell.items()):
    # one spelling target per word; `old` outranks `alt` if editions disagree
    t, kind = sorted(ts.items(), key=lambda kv: kv[1] != "old")[0]
    out_edges.append((w, "x", t, kind))

with open(f"{D}/de_lemmas.tsv", "w", encoding="utf-8") as f:
    # third column: which edition(s) gave it real senses — the filter trusts an
    # en-confirmed noun (`Falle`, `Mais`) over a de-only variant page (`Gedanken`)
    for w, p in sorted(lemmas):
        src = ("en" if (w, p) in real_en else "") + ("de" if (w, p) in real_de else "")
        f.write(f"{w}\t{p}\t{src}\n")
with open(f"{D}/de_formof.tsv", "w", encoding="utf-8") as f:
    for row in out_edges:
        f.write("\t".join(row) + "\n")
with open(f"{D}/de_forms.tsv", "w", encoding="utf-8") as f:
    for (w, p), fs in sorted(formtabs.items()):
        if (w, p) in lemmas:
            for fw in sorted(fs):
                f.write(f"{fw}\t{w}\t{p}\n")

kinds = defaultdict(int)
for *_, k in out_edges:
    kinds[k] += 1
print(f"lemmas {len(lemmas):,} · edges {dict(kinds)}", file=sys.stderr)
