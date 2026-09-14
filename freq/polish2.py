"""One consolidated cleanup pass over the built list.

Every rule here was written, run, and read before being kept. Three earlier attempts were
thrown out on their own output, which is the point of printing what a filter removes:

  * "absent from written German" flagged `abkriegen`, `krepieren`, `Volltrottel`, `Tusse` —
    colloquial words are missing from news and Wikipedia *because* they are colloquial.
  * "reduces to another word" over Wiktionary's `forms` field flagged `Junge→jung`,
    `Macht→machen`, `Abfahrt→abfahren` — derivation is not inflection; a noun built from a
    verb is separate vocabulary.
  * "has repeated letters" flagged `Fee`, `Aal`, `Ebbe`, `Popo`, `Mumm`, `Gag`, `rar`.
    A short German word simply has few distinct letters, and `Fitnessstudio`,
    `Schifffahrt`, `Nussschale` spell their triple consonants correctly.

Nominalised adjectives (`der Fremde`, `das Beste`, `die Süße`) are NOT removed: Wiktionary
gives them noun entries exactly as it does `Junge` and `Knast`, because German forms them
productively and they are real words. Only their declensions are collapsed into one row —
and the licence for "this is a nominalised adjective" is checked against the adjective's
FORMS as well as its lemma, because the 2026-08-24 audit found `Beste`/`Bestes`/`Bester`
all shipped separately: their stem `best` is no adjective headword (the lemma is `gut`),
but `beste` is one of its declined forms, and that is the licence that groups them.
"""
import os
import re
import sys
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(D))
sys.path.insert(0, D)
import foldlib

dic = foldlib.Dict(D)

from lib.vocab import migaku
mrows, _ = migaku.dump_rows()
MIG = {str(r[0]).lower() for r in mrows if str(r[3]).lower().startswith("de")}

rows = [l.rstrip("\n").split("\t") for l in open(f"{D}/de-spoken-final.tsv", encoding="utf-8")][1:]
freq = {r[1]: float(r[2]) for r in rows}
upos = {r[1]: r[3] for r in rows}
wikt = {r[1]: set(r[4].split(",")) - {""} for r in rows}
by_lower = defaultdict(list)
for r in rows:
    by_lower[r[1].lower()].append(r[1])
cut = {}

for w in freq:                                            # 1
    if len(w) <= 2:
        cut[w] = "one or two letters"
    elif any(c.isdigit() for c in w):
        cut[w] = "contains a digit"
    elif " " in w:
        cut[w] = "a phrase, not a word"
    elif re.search(r"[^A-Za-zÄÖÜäöüß\-'’éèêëáàâíìîóòôúùûçñœæ]", w):
        cut[w] = "not spelled with German letters"
    elif w.isupper() and len(w) >= 3:
        # LAPD, ATF, PTBS, GRU — dewikt gives abbreviations real pages, and emphatic
        # all-caps dialogue gives them counts; none of them is a word to study
        cut[w] = "an all-caps abbreviation"

for lo, vs in by_lower.items():                           # 2
    if len(vs) > 1:
        top = max(vs, key=lambda v: freq[v])
        for v in vs:
            if v != top and upos.get(v) == upos.get(top) and v not in cut:
                cut[v] = f"same word as {top}, differing only in case"

# 3 ── two exact spelling cases, and nothing looser. Adding `w+"e"`/`w+"en"` and a bare
#      strip of a trailing -s was tried and cut 94 real words: `Fass` reached `fassen`,
#      `Koks` reached `Kok`, `erstens` reached `erste`. A barrel is not the verb to grasp.
#      What is left: an apostrophe dropped from a VERB contraction (`gehts`, `gibts`,
#      `bins`), and ss written for ß where the ß spelling is itself already in Migaku.
for w in list(freq):
    if w in cut:
        continue
    if (w.endswith("s") and len(w) >= 4 and upos.get(w) in ("VERB", "AUX")
            and any(t.lower() in MIG for t in dic.fof_any.get(w[:-1], ()))):
        cut[w] = f"apostrophe dropped from {w[:-1]}'s"
        continue
    swap = w.replace("ss", "ß") if "ss" in w else (w.replace("ß", "ss") if "ß" in w else None)
    if swap and swap.lower() in MIG:
        cut[w] = f"the ss/ß spelling of {swap}, which is in Migaku"

# 4 ── declensions of one nominalised adjective, collapsed to the commonest.
#      The licence asks the dictionary whether the stem IS an adjective — as a lemma
#      (`jung` for Junge/Junges, `zehnjährig`) or as a declined form of one (`beste` →
#      gut, which is how Beste/Bestes/Bester group with no adjective named `best`).
#      A Swiss-spelt stem is normalised first (Weisser → weiß). -em is left out of the
#      endings: it collides with Ödem.
def adjish(stem):
    for s in (stem, dic.spell.get(stem, stem).lower()):
        if "adj" in dic.head.get(s, ()) or "adj" in dic.head.get(s + "e", ()):
            return True
        if dic.fof[s].get("adj") or dic.fof[s + "e"].get("adj"):
            return True
    return False

fam = defaultdict(list)
for w in freq:
    if w in cut or not w[:1].isupper():
        continue
    m = re.match(r"^(.*?)(e|er|es|en)$", w)
    if m and len(m.group(1)) >= 3 and adjish(m.group(1).lower()):
        fam[m.group(1).lower()].append(w)
for stem, vs in fam.items():
    if len(vs) > 1:
        top = max(vs, key=lambda v: freq[v])
        for v in vs:
            if v != top:
                cut[v] = f"same nominalised adjective as {top}"

# 5 ── `das Schmelzen` beside `schmelzen`: German nominalises any infinitive, so the noun
#      is not separate vocabulary. Restricted to a lowercase twin that is a VERB ending in
#      -en AND is the commoner of the two, which is what keeps `Verbrechen` (far commoner
#      than the verb `verbrechen`) and leaves `Heim`/`heim` alone — not an -en verb.
for lo, vs in by_lower.items():
    if len(vs) != 2:
        continue
    # PROPN counts as the noun side: hearing-impaired sound labels ("(KICHERN)") get
    # tagged PROPN, which is how `Kichern` sat beside `kichern` untouched
    noun = next((v for v in vs if v[:1].isupper() and upos.get(v) in ("NOUN", "PROPN")), None)
    # infinitives end -en, -ern, -eln (and tun): endswith("en") missed every -ern/-eln
    # verb, which is how Kichern, Zittern and Murmeln sat beside their verbs untouched
    verb = next((v for v in vs if upos.get(v) in ("VERB", "AUX") and v.endswith("n")), None)
    if noun and verb and noun not in cut and freq[verb] > freq[noun]:
        cut[noun] = f"nominalised infinitive of {verb}"

for w in freq:                                            # 6 — `grrr`, and only `grrr`
    if w not in cut and re.search(r"(.)\1\1", w.lower()) and wikt.get(w, set()) <= {"intj"}:
        cut[w] = "a noise, not a word"

# 7 ── the same word spelt with ß and with ss must not hold two rows (`fleißig` #87 and
#      Swiss `fleissig` #14821 both shipped; `Anlaß` #8385 beside `Anlass` #300). The
#      spelling edges catch these at resolution now; this is the in-list belt for pairs
#      neither edition marks, and the commoner spelling keeps the row.
for w in list(freq):
    if w in cut or "ß" not in w:
        continue
    for s in by_lower.get(w.replace("ß", "ss").lower(), ()):
        if s not in cut and freq.get(s) is not None:
            loser = w if freq[w] <= freq[s] else s
            cut[loser] = f"ß/ss doublet of {w if loser is s else s}"

g = defaultdict(list)
for w, why in cut.items():
    g[why.split(" as ")[0].split(" of ")[0]].append(w)
print(f"list {len(rows):,} → cutting {len(cut)}", file=sys.stderr)
for k, v in sorted(g.items(), key=lambda kv: -len(kv[1])):
    v.sort(key=lambda x: -freq[x])
    print(f"  {len(v):>3}  {k}\n       {', '.join(v[:24])}", file=sys.stderr)
keep = [r for r in rows if r[1] not in cut]
with open(f"{D}/de-spoken-final.tsv", "w", encoding="utf-8") as f:
    f.write("rank\tlemma\tper_million\tpos\twiktionary_pos\n")
    for i, r in enumerate(keep, 1):
        f.write("\t".join([str(i)] + r[1:]) + "\n")
with open(f"{D}/de-spoken-cut.tsv", "a", encoding="utf-8") as f:
    for w, why in cut.items():
        f.write(f"{w}\t{freq[w]:.2f}\t{why}\n")
print(f"\nkept {len(keep):,}", file=sys.stderr)
