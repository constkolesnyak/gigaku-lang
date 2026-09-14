"""Fill the dictionary gaps with `claude -p` — the words Wiktionary doesn't translate.

The ~4,000 words neither Wiktionary glosses are exactly the list's colloquial half:
prefixed spoken verbs (reingehen, rausholen), nominalised participles (Überlebende,
Geschworene), compounds. A dictionary lookup is the narrowest thing a model does
(lib/subs/translate.py's `_normalise` made the same call), so each request is a batch
of bare words, the reply is `id: перевод` lines matched on the echoed id — never by
position, per the project rule — and answers are appended to de-ru-fill.tsv after
every request, so a rate limit costs one request, not the run (lib/anki/score.py's
lesson). ru.py merges the file at lowest priority; re-running it after this finishes
is what puts the glosses into de-words.tsv.
"""
import os
import re
import sys

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(D))
from lib.claude import ask, describe

GROUP = 250
SYSTEM = (
    "Ты — немецко-русский словарь для изучающего разговорный немецкий. "
    "На каждую строку вида 'id: слово' ответь строкой 'id: перевод' — 1–3 русских "
    "эквивалента через запятую, кратко, без пояснений и без транскрипций. Слова — "
    "из частотного списка киносубтитров; выбирай самое употребительное значение. "
    "Отвечай ТОЛЬКО строками 'id: перевод', по одной на слово, ничего больше.")

done = set()
if os.path.exists(f"{D}/de-ru-fill.tsv"):
    for line in open(f"{D}/de-ru-fill.tsv", encoding="utf-8"):
        done.add(line.split("\t")[0])

gaps = []
for line in open(f"{D}/de-ru.tsv", encoding="utf-8"):
    p = line.rstrip("\n").split("\t")
    if len(p) == 2 and not p[1] and p[0] not in done:
        gaps.append(p[0])
print(f"{len(gaps):,} gaps to fill, {GROUP} per request", file=sys.stderr)

for i in range(0, len(gaps), GROUP):
    batch = gaps[i:i + GROUP]
    text = "\n".join(f"{j}: {w}" for j, w in enumerate(batch))
    reply, usage, _ = ask(text, SYSTEM, model="opus", what=f"batch {i // GROUP + 1}",
                          effort="low")
    got = {}
    for line in (reply or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*[:.]\s*(.+?)\s*$", line)
        if m and int(m.group(1)) < len(batch):
            got[int(m.group(1))] = m.group(2)
    with open(f"{D}/de-ru-fill.tsv", "a", encoding="utf-8") as f:
        for j, w in enumerate(batch):
            if j in got:
                f.write(f"{w}\t{got[j]}\n")
    print(f"batch {i // GROUP + 1}/{(len(gaps) + GROUP - 1) // GROUP}: "
          f"{len(got)}/{len(batch)} · {describe(usage)}", file=sys.stderr)
print("done", file=sys.stderr)
