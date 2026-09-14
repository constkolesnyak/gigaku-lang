"""The rubric and the response shaping. Pure — no subprocess, no Anki.

**The model is asked to un-know a word it knows.** That is the whole difficulty. A model
reading 閉じ込める knows what it means; the question is whether a learner who does *not*
could arrive at it from this sentence alone. Hence the blank-the-target test, the explicit
list of what does not count, and the worked examples — which are there mainly to hold the
low end of the scale, where the rubric is easiest to drift off.

**The examples are real cards, and that is a correction.** The first set was written by
hand, and three of its low anchors (`それはちょっと難しいですね。`, `まあそれが基本なんです
よね。`, `えー、手のひらを`) were three- to five-morph fragments — a population that
`CLARITY_MIN_MORPHS` filters out before a single one reaches the model. So the bottom of
the scale was anchored on cards that cannot occur, and the bottom of the *real* population
— long, fluent, and empty — had no anchor at all. Every example is now a card from the
collection, at or above the floor.

**The antonym check earned its place, unlike the pick line.** Reading the scored collection
turned up a class of generous score: 先生というのは一番最悪の仕事です came back 0.74, but
最高 fills that blank exactly as well — the sentence had handed over the *dimension*, not the
word. Same for 深くまで行けない (遠く fits) and 家もそんなに広くない (安く fits). The rule is
a specialization of the blank test rather than a competing dimension, which is why it does
not repeat the fragment-penalty disaster below, and it was A/B'd on two independent samples
before shipping: card by card the sentences that *name* the contrast rose (増えることはあって
も減ることはない 88→90, 昼暖かかったのに急に寒くなった 80→84) and the polarity-ambiguous
ones fell (ストレスが減ってますよね 50→30, 運動したとしても太ります 55→46), while the winner
survived a second ask in 15/15 words against 10/15 without it.

**One rule, not three.** An earlier version grew a separate penalty for sentences that
break off mid-thought, then a "band is binding" rule to stop that penalty fighting the
no-ties rule. All three pulled against each other and the scores swung 30–40 points between
identical runs. The penalty was redundant to begin with: a fragment with nothing in it
fails the blank test on its own, because *obviousness already presupposes there is
something in the sentence to be obvious from*. Removing it is what made the rest hold.

**A `BEST:` line per block was tried, and it did not earn its place.** What the score
decides is one card per word, so what has to be stable is the argmax — and it wasn't: over
118 scored words the top-two gap had a **median of 3 points** against ~6 points of re-ask
noise (one real block ran 0.82 · 0.81 · 0.80 · 0.79 · 0.77 … for 61 candidates). Eight
sentences a point apart is a queue, not a judgement, so each block was asked to end by
naming its clearest card, with the numbers then swapped to agree with it.

The model answered every block — and **named its own top-scoring card every single time**:
zero disagreements in 40 production blocks and 18 more under A/B, so the swap never once
fired. Asked head to head on the same 18 words, two asks apart, the rubric with the line
and the rubric without kept the same winner in **14/18 words each** — a dead heat — while
per-card noise was 7.0 points with it against 5.8 without. It bought nothing, so it is
gone. What is left of that investigation is this paragraph and the anchoring below, which
is the part that did change something.

The output contract is `id: score` lines, one per card, and it is deliberately not JSON:
scoring runs through the Claude Code CLI (see score.py), where nothing server-side is
enforcing a schema, and a group is hundreds of cards. A stray line then costs one card
instead of a malformed brace costing the whole group.
"""
import hashlib
import json
import re

# Scores come back as integers 0..SCALE and are stored as score/SCALE.
#
# 100, not 10, and the reason is the no-ties rule below: a word can have up to 103
# candidates in one block, so an 11-point scale cannot give them distinct values even in
# principle. 0..100 also lands exactly on the field ("0.00".."1.00") and on the sort key's
# three digits, so widening it costs nothing downstream.
SCALE = 100


SYSTEM = """\
You rate Japanese example sentences for a learner using i+1 flashcards.

Each item gives a SENTENCE and one TARGET word that occurs in it — inflected as the
sentence needs, so 繰り返す may appear as 繰り返して. Assume the learner knows every other
word in the sentence and that the TARGET is the single word they do not know.

Rate 0-100: **how much of the target's meaning does this sentence hand the learner?**

## What these sentences are

Transcribed speech from a conversational podcast: fillers (えー、あの、なんか), restarts,
and single turns lifted out of a conversation with nothing around them. That is the normal
case here, not a defect — and it is why so many of these reveal nothing. A speaker saying
そういう態度はやっぱり良くないと思う is leaning on context you cannot see, and neither can
the learner, who meets this sentence alone on a card.

Sentences too short to carry context are filtered out before they reach you. Every item you
see is long enough to have said something; most of them still haven't. Length is not
evidence, and a fluent, well-formed, entirely empty sentence is the single most common card
in this collection.

## The one test

Blank the target out and read what is left. Then ask: how many different words could fill
that blank and leave a natural, sensible sentence?

- If essentially any word of that class fits, the sentence tells the learner nothing about
  *this* word. Low.
- If the rest of the sentence narrows the blank to one idea, the learner can arrive at the
  meaning. High.

**Then check the opposite.** Run the same test against the target's antonym. If 最高 sits in
the blank as naturally as 最悪 does, the sentence has handed over the *dimension* — how good
a job is, how warm a day is, how much someone weighs — and not the word: that is the 30-45
band, however specific the rest of the sentence sounds. The reverse case is the top of the
scale, not the bottom: a sentence that *names* the opposite (話が得意な人と苦手な人っている)
is fixed by that contrast and belongs at 85+.

That is the whole judgement. Everything below is that test spelled out.

## What does not count

- **Kanji.** 明後日 gives itself away through its characters. That is not the sentence
  working, and it does not raise the score. Rate only what the surrounding words contribute.
- **How common or useful the word is.** You are not rating whether the card is worth
  studying, only how much this sentence reveals.
- **Grammar alone.** Knowing the target is a verb, or that it takes を, narrows meaning
  barely at all.
- **Length.** A long stretch of vague talk reveals less than a short concrete statement.
  Judge what the words say, not how many there are.
- **Your own knowledge.** You know the word; the learner does not. Simulate them. The
  question is never whether *you* understand the sentence.

## The scale

- **90-100** — the sentence effectively gives the meaning: a definition, a gloss, an
  apposition, a contrast that names the opposite, an enumeration placing it in its category.
- **70-85** — one idea survives the blank. The learner lands on the meaning or a close
  synonym, because the surrounding words only fit that one thing.
- **50-65** — the sentence pins the target to a small family of related meanings. Real
  information, not yet the word.
- **30-45** — category only: the learner can tell it is a food, a place, a stretch of time,
  a feeling, a way of moving, but not which one.
- **15-25** — role only: part of speech, maybe register. Nothing about the meaning.
- **0-10** — the slot takes almost anything. Typically the target is the only thing in the
  sentence carrying content, so removing it leaves a frame with no information in it.

Judge the content, then place it. A sentence that reveals nothing scores in the single
digits **however it compares to its neighbours** — see the block rule below.

## Examples

SENTENCE: つまり、毎日同じことを繰り返すこと、それが習慣です。
TARGET: 習慣
SCORE: 96 — the sentence states the definition outright.

SENTENCE: パーソナリティー、ね、性格がそれぞれの子どもには性格があるから、
TARGET: 性格
SCORE: 92 — パーソナリティー sits in apposition to it. That is a gloss, not context, but it
is the sentence doing the work.

SENTENCE: 田舎が嫌いだから都会に出てきたはずなんですが、
TARGET: 都会
SCORE: 88 — 田舎 names the opposite and 出てきた gives the direction.

SENTENCE: サッカーとテニス両方やってたんですよね。
TARGET: 両方
SCORE: 84 — exactly two things enumerated, then the target. Only "both" fits.

SENTENCE: 何度も何度も同じような間違いを繰り返してしまう。
TARGET: 繰り返す
SCORE: 78 — 何度も何度も and 同じような between them force "repeat".

SENTENCE: 僕はいつもそうやって目標とか目的を
TARGET: 目標
SCORE: 62 — coordinated with 目的, so it is something one sets and aims at; which shade of
it the learner still has to guess.

SENTENCE: どちらかというとネガティブな感情がありますよね。
TARGET: 感情
SCORE: 55 — ネガティブな narrows it to something that can be negative and be "had": a small
family of inner states.

SENTENCE: 学校教育で外国語を勉強するときに、
TARGET: 教育
SCORE: 50 — 学校〜 and the studying frame put it in the right area without naming it;
学校制度, 学校生活 and others fit the blank equally.

SENTENCE: なんかどうでもいいような態度を取る、
TARGET: 態度
SCORE: 42 — 〜を取る plus どうでもいいような makes it a stance one adopts. Which one is the
learner's guess.

SENTENCE: ちょっと親がコントロールする必要がありますけど。
TARGET: 親
SCORE: 24 — someone with authority over someone else. 先生 or 会社 would sit here just as
well.

SENTENCE: えー、まあ、その人の日本語は上達しません。
TARGET: 上達
SCORE: 22 — negated, applied to 日本語: something a language can fail to do. Improve, change,
stick — all survive.

SENTENCE: 多分美しさとかそういうことだと思いますね。
TARGET: 美しい
SCORE: 8 — 〜とかそういうこと is a shrug. Long, fluent, and it says nothing at all.

SENTENCE: 態度はやっぱ大事なんですよね、
TARGET: 態度
SCORE: 3 — ___は大事 takes any noun in the language. The target carries the whole sentence.

## Cards come in blocks, one block per word

Items arrive under a WORD heading. Every line in a block is a competing sentence for that
same word, and choosing the best one is what these numbers are for. Read a block together.

**Two rules, in this order.**

1. **Content sets the number.** Place each sentence on the scale by what it reveals, exactly
   as you would if it arrived alone. A block of uniformly empty sentences comes back 4, 5,
   6, 7 — never 30, 50, 70, 90. Being the best line of a weak block earns nothing.

2. **Then break ties.** No two sentences in a block may carry the same number. When two
   look equal, look again: one names the situation more concretely, or leaves fewer
   readings open. Give that one the higher number — **one point is enough**. Separating by
   a single point inside a band is right; jumping a band to separate is wrong.

## Worked block

    WORD: 態度  (4 sentences)
    ID: 1712000000001  SENTENCE: 態度はやっぱ大事なんですよね、
    ID: 1712000000002  SENTENCE: バカにしていたりっていう態度がですね、
    ID: 1712000000003  SENTENCE: で、そういう態度をしていたことに気がついたんですよね。
    ID: 1712000000004  SENTENCE: 周りの人のを全然気にしない態度、

    1712000000001: 3
    1712000000002: 55
    1712000000003: 12
    1712000000004: 50

Every line here is about 態度 and the block still spans 3 to 55: two of them name a concrete
stance (mocking someone, ignoring everyone around you) and two are frames. That spread is
the normal shape of a block — do not compress it, and do not inflate the weak ones because
they are keeping company with a strong one.

## Output

One line per item, in the order given: the ID, a colon, a space, the score. Nothing else —
no preamble, no headings, no reasoning, no totals.

Score every item exactly once; never merge, reorder or drop one. Decide from the sentence
in front of you and move on: this is a fast pass over hundreds of cards, and deliberating
at length on each is wasted effort.\
"""

# Short fingerprint of the rubric above. Stamped on a run (store.start_run) so a card is
# traceable to the wording that judged it, and so a run can say out loud that the rubric
# moved — old and new scores inside *one word* are not comparable, and that word is exactly
# where they get compared.
FINGERPRINT = hashlib.sha1(SYSTEM.encode()).hexdigest()[:8]


def rubric(lang):
    """(system, fingerprint) for one language.

    Japanese is this module's own SYSTEM; every other language is a *named* sibling
    module (prompt_de) — translate_prompt.py's rule: a second language pair means a
    second rubric, never a parameterised one. Unknown languages fail in config's
    clarity_profile before this is ever asked.
    """
    if lang == "de":
        from lib.anki import prompt_de

        return prompt_de.SYSTEM, prompt_de.FINGERPRINT
    return SYSTEM, FINGERPRINT


def render(items):
    """The user turn for one group, blocked by word. `items` are (id, sentence, morph).

    Blocking is load-bearing, not cosmetic. Scored independently on an absolute scale, a
    word's candidates come back nearly tied — measured: median top-two gap 1.0 point with
    47% of words tied outright, against 1.44 points of re-ask noise, which makes the
    argmax pick a coin flip. Laid side by side under one heading the judgement becomes
    comparative, which is both what the score is used for and the thing models are steady
    at.

    Both counts — items in the group, sentences in each block — are stated because a reply
    that comes back short is otherwise only detectable by counting it afterwards, and the
    model cannot count what it was never told.
    """
    # Block boundaries compare select.fold()ed morphs: Anki treats Leben/leben as one
    # word (case-insensitive search), so a case-sensitive boundary here split that word
    # into two blocks — the two-half-comparisons defect this function exists to prevent.
    # The heading shows the first card's own casing.
    from lib.anki.select import fold

    blocks, current, word, shown = [], [], None, None
    for item_id, sentence, morph in items:
        if fold(morph) != word:
            if current:
                blocks.append((shown, current))
            word, shown, current = fold(morph), morph, []
        current.append(f"ID: {item_id}  SENTENCE: {sentence}")
    if current:
        blocks.append((shown, current))
    body = "\n\n".join(
        f"WORD: {w}  ({len(lines)} sentence{'' if len(lines) == 1 else 's'})\n"
        + "\n".join(lines)
        for w, lines in blocks
    )
    return (
        f"Rate all {len(items)} items below, in {len(blocks)} "
        f"block{'' if len(blocks) == 1 else 's'}.\n"
        f"Answer with {len(items)} score lines.\n\n" + body
    )


def parse(text, expected_ids):
    """``id: score`` lines → ``{id: score}``, keeping only ids we actually asked for.

    Matching is on the echoed id, never on position, so a reply that comes back short,
    reordered, or with an invented id degrades into "these ones are missing" — which the
    caller re-asks once — instead of silently attaching scores to the wrong cards.

    Line-based rather than JSON because there is no server-side schema enforcing the shape
    here: one malformed brace would cost the whole group, whereas a stray line costs one
    card. A JSON body is still accepted, since a model asked for pairs sometimes gives you
    JSON anyway.
    """
    wanted = set(expected_ids)
    out = {}

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict):
        for entry in data.get("scores", []):
            try:
                item_id, score = int(entry["id"]), int(entry["score"])
            except (KeyError, TypeError, ValueError):
                continue
            if item_id in wanted and 0 <= score <= SCALE:
                out.setdefault(item_id, score)
        if out:
            return out

    # Note ids are 13-digit epochs, scores are 0..100 — the two can't be confused, and
    # anchoring the id at a line start keeps prose from being mined for pairs.
    for match in re.finditer(r"^\D{0,4}(\d{6,})\s*[:.\-\t]\s*(\d{1,3})\b", text, re.M):
        item_id, score = int(match.group(1)), int(match.group(2))
        if item_id in wanted and 0 <= score <= SCALE:
            out.setdefault(item_id, score)
    return out


def groups(items, size):
    """Split items into request-sized groups, **never splitting a word**.

    Size is the cost lever, and it points the *opposite* way from a raw API call: every
    `claude -p` invocation drags Claude Code's own ~24k-token harness prompt along with
    it — measured, and irreducible, since a bare "say OK" costs the same. So the harness
    is what needs amortising, not the rubric, and groups want to be hundreds of cards
    rather than the couple of dozen an API request would take.

    **A word is never split**, because its candidates are rated against each other (see
    `render`) and half of them in the next call is two half-comparisons. Words are packed
    greedily instead: a new group starts as soon as the next word would not fit.

    That leaves `size` a real bound only if no single word exceeds it, which is what
    `select.candidates`' ceiling guarantees — it trims an oversized word by dropping its
    shortest sentences. An unbounded group is how a run dies: a 409-card group (two whole
    words, 149 + 260) produced 54k output tokens and the call after it failed outright.
    The earlier version flushed only *at* a word boundary and only once already past
    `size`, so it happily ran two whole words together — hence the bound being checked
    before appending a word rather than after.
    """
    size = max(1, int(size))
    out, current, run = [], [], []

    def close_run():
        nonlocal current, run
        if not run:
            return
        if current and len(current) + len(run) > size:
            out.append(current)
            current = []
        current += run
        run = []

    from lib.anki.select import fold

    last = None
    for item in items:
        if fold(item[2]) != last:  # folded like render()'s blocks — Leben/leben is one word
            close_run()
            last = fold(item[2])
        run.append(item)
    close_run()
    if current:
        out.append(current)
    return out
