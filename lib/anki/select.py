"""Which cards to score, and the statistics `--calibrate` reports. Pure — no SDK, no Anki.

The whole cost story is here, and it is a two-stage funnel.

Scoring every i+1 card is 74,801 calls' worth of sentences. But you never need a word's
alternates *ranked* — you need its best example surfaced. So `am-all-morphs-count`, the
free proxy the collection already carries, picks the top few candidates per word, and the
model only ranks those. Measured on the live collection: threshold 5, K=5 takes the
backlog to ~15,600 cards while still covering 4,645 of the 5,000 words that have
alternates at all.

Two populations are deliberately skipped:

* **singletons** — 2,176 i+1 cards are the only card for their word, so there is nothing
  to rank and a score would change no ordering;
* **sub-threshold cards** — their field is left *empty* rather than written as 0. Empty
  and "0.00" both sort as ``000`` in My Sort Key, so writing them would be thousands of
  collection writes to express a decision the selection already re-derives every run.
"""
import hashlib
import random
import re
from dataclasses import dataclass

# Buckets above this are pooled in the calibration table — measured max is 27, but the
# tail past 15 is 165 cards total and would give per-bucket samples of one or two.
MAX_BUCKET = 15


@dataclass(frozen=True)
class Card:
    note_id: int
    sentence: str
    morph: str
    all_count: int
    clarity: str = ""     # the My Clarity field as it stands; "" = never scored
    # My Alternatives and My Sort Key, both maintained by the add-on. Carried so a scoring
    # run can refresh the sort key itself instead of leaving a stale one until the next
    # recalc — see store.sort_key for why that is a convenience and not a second owner.
    alternates: int = 0
    sort: str = ""
    run: str = ""       # which gigaku clarity pass last scored it

    @property
    def key(self):
        """Cache key: the judgement depends on the sentence and the word, not the note id.

        Keying on content is what makes a re-import, a cleared field, or a card leaving
        and re-entering i+1 free instead of a second bill for the same question.
        """
        return hashlib.sha1(
            f"{self.sentence}\x1f{self.morph}".encode()
        ).hexdigest()


def from_notes(records, *, sentence_field, morph_field, allcount_field, clarity_field,
               alternates_field="My Alternatives", sort_field="My Sort Key",
               run_field="My Run"):
    """AnkiConnect `notesInfo` records → Cards, dropping anything unscoreable."""
    cards = []
    for record in records:
        fields = record.get("fields") or {}

        def value(name):
            return (fields.get(name) or {}).get("value", "").strip()

        sentence, morph = value(sentence_field), value(morph_field)
        if not sentence or not morph:
            continue
        try:
            all_count = int(value(allcount_field) or 0)
        except ValueError:
            all_count = 0
        try:
            alternates = int(value(alternates_field) or 0)
        except ValueError:
            alternates = 0
        cards.append(Card(
            note_id=record["noteId"],
            sentence=sentence,
            morph=morph,
            all_count=all_count,
            clarity=value(clarity_field),
            alternates=alternates,
            sort=value(sort_field),
            run=value(run_field),
        ))
    return cards


def fold(word):
    """The case key every grouping shares — one rule with the add-on's `gigaku.rules.fold`,
    pinned by tests/test_addon_rules.py.

    `str.lower`, NOT casefold (ß must survive — casefold writes strasse, a form no source
    produces). Anki's field search is case-insensitive, so L shows Leben and leben as one
    word; a case-sensitive grouping here sent them to the model as two blocks and picked
    two winners. Japanese has no case — every fold there is the identity. Keys fold, the
    displayed morph keeps its case.
    """
    return (word or "").lower()


def norm_morph(raw):
    """Bare word behind an am-study-morphs value — one rule with the add-on's, pinned by
    tests/test_addon_rules.py against `gigaku.rules.norm_morph`.

    MvJ writes the field in four shapes: bare ``撮る``, ``Sentence: 撮る``,
    ``Definition: 写す``, and — after a 💣 generates a definition carrying unknowns — the
    composite ``Sentence: 撮る | Definition: 写す``. The composite is the shape this used
    to miss: stripping only a leading prefix left ``撮る | Definition: 写す``, so the CLI
    keyed a 💣'd card as its own word and `_retag` could tag a second card for a word the
    add-on already had. The sentence morph is the word being studied, so it wins; a
    definition-only field falls back to that.
    """
    m = (raw or "").strip()
    if "Sentence:" not in m and "Definition:" not in m:
        return m
    sentence = definition = ""
    for part in m.split("|"):
        part = part.strip()
        if part.startswith("Sentence:"):
            sentence = part[len("Sentence:"):].strip()
        elif part.startswith("Definition:"):
            definition = part[len("Definition:"):].strip()
    return (sentence or definition).strip()


# A line that finishes: a full stop, or a sentence-final form. The corpus is subtitle-split
# speech, so a clause that runs on into the next line ends in 、 or a connective and fails
# this. Deliberately a mirror of the add-on's copy, like the sort key — one owner of the
# rule, two implementations, and tests pin the properties both must have. (Once a tiebreak
# in the my-learn pick; removed from it 2026-08 by the user's decision — see learn_winners.)
_SENTENCE_END = re.compile(
    r"(?:[。！？♪]"
    r"|[ぁ-んァ-ヶ一-龥](?:です|ます|ました|ません|でした|だ|ね|よ|な|わ|ぞ|かな|かも|でしょ"
    r"|でしょう|んです|んだ|のだ|ですね|ますね|ですよ|ますよ|よね|ですか|ますか|ください"
    r"|なさい)"
    r")[。！？…]?$"
)


def whole_sentence(text):
    """Does this card show a finished sentence rather than a clause cut out of one?"""
    return bool(_SENTENCE_END.search((text or "").strip()))


def learn_winners(cards, scores, *, studying=frozenset()):
    """One note id per word: its clearest card. Mirrors the add-on's `my-learn` rule
    (`gigaku.rules.learn_pick` — pinned equal by tests/test_addon_rules.py).

    **Highest clarity, full stop** — then am-all-morphs-count, then lowest note id so the
    pick is stable. Clarity leads because that is what the score is *for*. An unscored
    card ranks as 0, which is exactly the old length-only rule, so a partially scored
    collection degrades gracefully rather than shuffling.

    History: a "finished sentence wins inside the ~6-point re-ask noise band" tiebreak
    lived here for a while (measured in: 42 of 156 words moved, the queue went 43% → 75%
    whole sentences) and was removed on the user's decision 2026-08 — a tag on anything
    but the top-clarity card read as wrong in the alternates view, and that view is where
    the tag is judged.

    `studying` holds words that already have a card in the Study deck — as `fold`ed keys,
    built that way by both callers; they are skipped entirely, same as the add-on.
    """
    groups = {}
    for card in cards:
        word = fold(norm_morph(card.morph))
        if not word or word in studying:
            continue
        groups.setdefault(word, []).append(card)

    winners = set()
    for group in groups.values():
        winners.add(max(group, key=lambda c: (
            scores.get(c.key, 0),
            c.all_count,
            -c.note_id,
        )).note_id)
    return winners


def by_word(cards):
    """Group by the `fold`ed am-study-morphs value — exactly what L *shows*, since Anki's
    search is case-insensitive: Emoji (11 cards) and emoji (2) are one 13-card view, and
    grouping them apart sent the model two half-comparisons and picked two winners."""
    groups = {}
    for card in cards:
        groups.setdefault(fold(card.morph), []).append(card)
    return groups


def word_order(groups):
    """Words in study order: biggest alternate group first, ties broken for determinism.

    Group size is My Alternatives — how many cards in the deck teach this word — which is
    the order the study queue is sorted by. Scoring in that order means a word's
    alternates are ready by the time you reach it.
    """
    return sorted(groups, key=lambda morph: (-len(groups[morph]), morph))


def candidates(group, *, min_morphs, per_word, ceiling=0):
    """A word's scoreable alternates: above the floor, ordered by the free proxy.

    `per_word` caps them on purpose (0 = judge them all). `ceiling` is a different thing —
    an overflow guard so a word always fits in one request, because the candidates are
    rated *against each other* and a word split across two calls is two half-comparisons.
    When a word overflows, the cut goes by am-all-morphs-count: the shortest sentences are
    the ones least likely to win (measured: under 4 morphs, 1 winner in 158), so they are
    what to drop. Only 16 words in the collection are big enough to be cut at all.
    """
    eligible = [c for c in group if c.all_count >= min_morphs]
    eligible.sort(key=lambda c: (-c.all_count, c.note_id))
    if per_word > 0:
        eligible = eligible[:per_word]
    if ceiling > 0:
        eligible = eligible[:ceiling]
    return eligible


def select(cards, *, min_morphs=0, per_word=5, words=0, scored=frozenset(), ceiling=0):
    """Cards to send, newest budget first. Returns (cards, words_touched).

    `words` is the budget and it counts only words that actually cost something: a word
    whose candidates are all cached already is skipped without consuming it, so a re-run
    after a partial pass advances instead of re-walking the same head of the queue.

    **A word goes whole or not at all.** If any of its candidates needs scoring, all of
    them are sent, including ones the cache already holds. Sending only the uncached ones
    is cheaper by exactly the wrong thing: the candidates are rated *against each other*,
    so a card scored alone carries a number from a different comparison, and the `my-learn`
    pick then compares it with the rest of the block as though they were commensurable.
    That is the same defect `prompt.groups` refuses to create and the re-ask in
    `clarity._run` was corrected for; it arrives here through a new card joining a word
    that was scored last month. Measured when this was written: 0 of 153 cached words were
    half-cached, so closing it cost nothing — it is the *next* imported episode this is
    for.
    """
    groups = by_word(cards)
    chosen, touched = [], 0
    for morph in word_order(groups):
        group = groups[morph]
        if len(group) < 2:
            continue  # singleton: no alternates to rank
        pool = candidates(group, min_morphs=min_morphs, per_word=per_word,
                          ceiling=ceiling)
        fresh = pool if any(c.key not in scored for c in pool) else []
        if not fresh:
            continue
        if words and touched >= words:
            break
        chosen.extend(fresh)
        touched += 1
    return chosen, touched


def scoreable(cards, *, min_morphs=0, per_word=5, ceiling=0):
    """Every card the current settings would ever score — for "N of M done" reporting."""
    groups = by_word(cards)
    out = []
    for group in groups.values():
        if len(group) < 2:
            continue
        out.extend(candidates(group, min_morphs=min_morphs, per_word=per_word,
                              ceiling=ceiling))
    return out


# ── calibration ──────────────────────────────────────────────────────────────────────

def bucket(all_count):
    return min(max(int(all_count), 0), MAX_BUCKET)


def stratified(cards, per_bucket, *, seed=0):
    """Even sample across am-all-morphs-count buckets, over the alternates population.

    Even, not proportional: the question is what each bucket is *worth*, and a
    proportional sample would put a handful of cards in exactly the thin low buckets the
    threshold has to be drawn between.
    """
    groups = by_word(cards)
    pool = [c for group in groups.values() if len(group) >= 2 for c in group]
    buckets = {}
    for card in pool:
        buckets.setdefault(bucket(card.all_count), []).append(card)
    rng = random.Random(seed)
    out = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda c: c.note_id)
        rng.shuffle(group)
        out.extend(group[:per_bucket])
    return out


def bucket_rows(population, scores):
    """The `--calibrate` table: is a sentence with N morphs worth asking about?

    `scores` maps note id → 0..1. `share` is the fraction of the bucket's sample that came
    back at or above HIGH — the yield that has to justify the call.
    """
    HIGH = 0.7
    pool = [c for group in by_word(population).values() if len(group) >= 2 for c in group]
    at = {}
    for card in pool:
        at[bucket(card.all_count)] = at.get(bucket(card.all_count), 0) + 1

    sampled = {}
    for card in pool:
        if card.note_id in scores:
            sampled.setdefault(bucket(card.all_count), []).append(scores[card.note_id])

    rows = []
    total_above = 0
    for key in sorted(at, reverse=True):
        total_above += at[key]
        values = sampled.get(key, [])
        rows.append({
            "morphs": key,
            "sampled": len(values),
            "mean": sum(values) / len(values) if values else None,
            "share_high": (
                sum(1 for v in values if v >= HIGH) / len(values) if values else None
            ),
            "cards": at[key],
            "cards_at_or_above": total_above,
        })
    return list(reversed(rows))


def recommend(rows, *, floor=0.05):
    """Lowest bucket whose high-score yield clears `floor`, and that every bucket above
    also clears — one thin bucket dipping under shouldn't cut off everything beneath it."""
    best = None
    for row in sorted(rows, key=lambda r: -r["morphs"]):
        if row["share_high"] is None:
            continue
        if row["share_high"] < floor:
            break
        best = row["morphs"]
    return best


def spearman(pairs):
    """Rank correlation, ties averaged. Answers the question that decides whether any of
    this is worth doing: does the model just reproduce am-all-morphs-count?

    If clarity and the proxy agree perfectly within a word, the collection already sorts
    alternates that way and the feature buys nothing.
    """
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(pairs) < 3:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    xs, ys = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if not dx or not dy:
        return None
    return num / (dx * dy)
