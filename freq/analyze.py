"""Compare the seven normalised lists, and compare all of them with the user's known German."""
import json
import os
import subprocess

D = os.path.dirname(os.path.abspath(__file__))
L = json.load(open(f"{D}/lists.json", encoding="utf-8"))

SPOKEN = ["OS2018", "SUBTLEX"]
WRITTEN = ["DEREKO", "NEWS", "WIKI"]
MAIN = ["OS2018", "SUBTLEX", "WORDFREQ", "WEB", "NEWS", "DEREKO", "WIKI"]

# ------------------------------------------------------------------ known words
raw = subprocess.run(["gigaku", "words", "--lang", "de", "--stage", "known"],
                     capture_output=True, text=True).stdout.split()
lemcache = json.load(open(os.path.expanduser(
    "~/Library/Caches/gigaku/de_lemmas.json"), encoding="utf-8"))
import simplemma
# The known set is put through the SAME lemmatiser as the lists, or the comparison would
# be measuring the two normalisations against each other rather than the vocabularies.
KNOWN = ({w.lower() for w in raw}
         | {lemcache[w].lower() for w in raw if w in lemcache}
         | {simplemma.lemmatize(w, lang="de").lower() for w in raw}
         | {simplemma.lemmatize(lemcache[w], lang="de").lower() for w in raw if w in lemcache})
print(f"known German words: {len(raw):,} cache forms → {len(KNOWN):,} matchable keys\n")

ranks = {k: {w: i + 1 for i, (w, _) in
             enumerate(sorted(d.items(), key=lambda kv: -kv[1]))} for k, d in L.items()}
tops = {k: [w for w, _ in sorted(d.items(), key=lambda kv: -kv[1])] for k, d in L.items()}


def spearman(a, b, n=20000):
    common = [w for w in tops[a][:n] if w in ranks[b]]
    if len(common) < 100:
        return float("nan")
    xs = sorted(common, key=lambda w: ranks[a][w])
    ys = sorted(common, key=lambda w: ranks[b][w])
    rx = {w: i for i, w in enumerate(xs)}
    ry = {w: i for i, w in enumerate(ys)}
    m = len(common)
    d2 = sum((rx[w] - ry[w]) ** 2 for w in common)
    return 1 - 6 * d2 / (m * (m * m - 1))


def jaccard(a, b, n):
    A, B = set(tops[a][:n]), set(tops[b][:n])
    return len(A & B) / len(A | B)


print("=" * 78)
print("1. SIZE AND SHAPE")
print("=" * 78)
print(f"{'list':10} {'lemmas':>8} {'top-1k known':>13} {'top-5k known':>13} "
      f"{'top-10k known':>14} {'text cov.':>10}")
for k in MAIN:
    row = []
    for n in (1000, 5000, 10000):
        t = tops[k][:n]
        row.append(f"{100 * sum(w in KNOWN for w in t) / len(t):.1f}%")
    cov = sum(v for w, v in L[k].items() if w in KNOWN) / sum(L[k].values())
    print(f"{k:10} {len(L[k]):>8,} {row[0]:>13} {row[1]:>13} {row[2]:>14} {100*cov:>9.1f}%")

print()
print("=" * 78)
print("2. HOW ALIKE ARE THEY  (Spearman on the top 20k / Jaccard of the top 5k)")
print("=" * 78)
print(f"{'':10}" + "".join(f"{k:>10}" for k in MAIN))
for a in MAIN:
    cells = []
    for b in MAIN:
        cells.append("    —     " if a == b else f"{spearman(a, b):>10.2f}")
    print(f"{a:10}" + "".join(cells))
print()
print(f"{'':10}" + "".join(f"{k:>10}" for k in MAIN))
for a in MAIN:
    cells = ["    —     " if a == b else f"{jaccard(a, b, 5000):>10.2f}" for b in MAIN]
    print(f"{a:10}" + "".join(cells))

# ------------------------------------------------------------------ spoken axis
import math

common = set(L["OS2018"]) & set(L["SUBTLEX"]) & set(L["DEREKO"]) & set(L["NEWS"]) & set(L["WIKI"])


def gmean(k_list, w):
    return math.exp(sum(math.log(max(L[k].get(w, 0), 1e-4)) for k in k_list) / len(k_list))


skew = {}
for w in common:
    s, r = gmean(SPOKEN, w), gmean(WRITTEN, w)
    if max(s, r) < 1.0:            # below ~1 per million in both — too rare to judge
        continue
    skew[w] = math.log2(s / r)

by_skew = sorted(skew.items(), key=lambda kv: -kv[1])
print()
print("=" * 78)
print("3. THE SPOKEN↔WRITTEN AXIS  (log2 of subtitle freq ÷ news+wiki+DeReKo freq)")
print("=" * 78)
print("\nMost spoken-skewed lemmas (top 40):")
for i in range(0, 40, 4):
    print("   " + "".join(f"{w:<16}{v:>5.1f}   " for w, v in by_skew[i:i + 4]))
print("\nMost written-skewed lemmas (top 40):")
tail = by_skew[-40:][::-1]
for i in range(0, 40, 4):
    print("   " + "".join(f"{w:<16}{v:>5.1f}   " for w, v in tail[i:i + 4]))

print("\nWhat share of each band the user already knows:")
bands = [("very spoken   (>+2)", [w for w, v in skew.items() if v > 2]),
         ("spoken        (+1..+2)", [w for w, v in skew.items() if 1 < v <= 2]),
         ("neutral       (-1..+1)", [w for w, v in skew.items() if -1 <= v <= 1]),
         ("written       (-2..-1)", [w for w, v in skew.items() if -2 <= v < -1]),
         ("very written  (<-2)", [w for w, v in skew.items() if v < -2])]
for name, ws in bands:
    if ws:
        print(f"  {name:24} n={len(ws):>6,}   known {100*sum(w in KNOWN for w in ws)/len(ws):>5.1f}%")

# ------------------------------------------------------------------ gaps
print()
print("=" * 78)
print("4. WHAT THE USER DOES NOT KNOW YET, by list (most frequent first)")
print("=" * 78)
gaps = {}
for k in MAIN:
    gaps[k] = [w for w in tops[k][:12000] if w not in KNOWN]
    print(f"\n{k}  — {len(gaps[k]):,} unknown in its top 12k. First 30:")
    print("   " + ", ".join(gaps[k][:30]))

# words unknown AND spoken-skewed — the actionable list
act = [(w, skew[w], L["OS2018"].get(w, 0)) for w in skew
       if w not in KNOWN and skew[w] > 0.5 and L["OS2018"].get(w, 0) > 0]
act.sort(key=lambda t: -t[2])
print()
print("=" * 78)
print(f"5. UNKNOWN *AND* SPOKEN-SKEWED — {len(act):,} lemmas. Top 60 by subtitle frequency")
print("=" * 78)
for i in range(0, 60, 3):
    print("   " + "".join(f"{w:<20}{f:>7.1f}/M  skew{s:>5.1f}   " for w, s, f in act[i:i + 3]))

json.dump({"skew": skew, "gaps": gaps,
           "actionable": [{"lemma": w, "skew": s, "os2018_pm": f} for w, s, f in act]},
          open(f"{D}/analysis.json", "w", encoding="utf-8"), ensure_ascii=False)
