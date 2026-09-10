"""Where the audio trimmer's end marker opens — the decision, pure, no aqt and no pydub.

The feature side (features/trimmer.py) supplies `speech(min_silence_ms)`, a detector over
the real audio; this module owns the ladder walk and every guard, so the measured behaviour
below is pinned by plain pytest instead of being testable only inside Anki.

A gap of a rung's length between two words is a pause rather than ordinary articulation;
whatever follows it is only "a trailing word" while it stays under TRAILING_MAX_MS and the
speech before it is at least KEEP_RATIO times as long (and MIN_KEEP_MS outright). With no
such word, the end of speech is remeasured at TAIL_SILENCE_MS, the shortest end-pause worth
noticing. A cut that would gain less than MIN_GAIN_MS isn't worth pre-empting a decision
over.

**The pause in front of a trailing word is routinely shorter than 350ms**, which is why the
structure is measured at a *ladder* of thresholds rather than at one: a single 350ms pass
merges such a word into the sentence and offers nothing. Measured over 150 of the
collection's clips: 350ms alone offers a cut on 84, and 300/275/250 find a further
6/13/15 — real trailing words behind pauses of 250–340ms. The ladder runs **coarse first
and stops at the first rung whose tail passes the guards**: no clip that already worked can
change (a finer rung is only reached when every coarser one declined), and the *whole* word
is cut rather than the end of it (a finer pass splits some tails in two — one 685ms tail
comes back as 340ms at 250). 250ms is the floor because a geminate stop (っ) or a plosive
closure *inside* a word is 150–250ms of near-silence: below that the detector is splitting
words, not finding pauses. Replayed over those 150 clips, ladder against single pass:
**84 offers → 99, none lost.**

TAIL_PAD_MS leaves a small pause after the last word kept, measured from where RMS drops
below the threshold — *earlier* than where a listener stops hearing the word, so it is not
all silence. At 120ms the cut had no audible pause left; 250 restores one while costing 2
of 36 sample clips (they already end with enough pause — the right answer, not a loss). It
can never eat into the trailing word: that branch caps the cut at the word's start.
"""

PAUSE_LADDER = (350, 300, 275, 250)
TRAILING_MAX_MS = 1500
KEEP_RATIO = 2
MIN_KEEP_MS = 500
TAIL_SILENCE_MS = 200
TAIL_PAD_MS = 250
MIN_GAIN_MS = 150


def _trailing_word(speech, coarse):
    """`(keep_until, limit, reason)` for a trailing word worth cutting, or None.

    The ladder walk: coarse first, stopping at the first rung whose tail passes the
    guards."""
    for pause in PAUSE_LADDER:
        rungs = coarse if pause == PAUSE_LADDER[0] else speech(pause)
        if len(rungs) < 2:
            continue
        last_start, last_end = rungs[-1]
        trailing = last_end - last_start
        kept_speech = sum(end - start for start, end in rungs[:-1])
        if trailing <= TRAILING_MAX_MS and kept_speech >= max(KEEP_RATIO * trailing, MIN_KEEP_MS):
            keep_until = rungs[-2][1]
            return (keep_until, last_start,
                    f"trailing {trailing}ms word behind a {last_start - keep_until}ms pause")
    return None


def end_cut(speech, audio_len):
    """`(cut_ms, reason)` where the end marker should open, or None to leave it alone.

    `speech(min_silence_ms)` → [(start_ms, end_ms), …] of non-silent stretches; called
    lazily, one rung at a time, so a clip the coarsest rung already answers costs one pass.

    **What is offered is decided by how the clip ends** (the user's rule): a pause at the
    end is what there is to cut, and the last word is only offered when the clip stops on
    it. Before this, a trailing word was cut whenever the ladder found one — including on
    clips that ended with a second of silence *after* it, where the silence is the obvious
    waste and taking the word as well is a bigger decision than a preset should make.

    The ladder is unchanged and still earns its rungs; it is now reached only for a clip
    that ends flush with speech, which is exactly where a trailing word is the thing left
    to cut. (The 84 → 99 measurement below is about *finding* such words at all, and still
    holds; how many of those 99 clips also end flush has not been re-measured.)"""
    chunks = speech(PAUSE_LADDER[0])
    if not chunks:
        return None
    # The fine pass, not a structural rung: a coarse pass absorbs an end-pause shorter than
    # itself into the last chunk and reports the clip as ending flush with speech (a third
    # of the sample looked so), which would read as "the word runs to the end" here.
    end_of_speech = (speech(TAIL_SILENCE_MS) or chunks)[-1][1]
    tail = audio_len - end_of_speech
    if tail >= TAIL_SILENCE_MS:
        keep_until, limit, reason = end_of_speech, audio_len, f"{tail}ms of trailing silence"
    else:
        offer = _trailing_word(speech, chunks)
        if offer is None:
            # Nothing behind a pause, and no end-pause worth noticing: TAIL_PAD_MS alone
            # would cut less than it keeps, and MIN_GAIN_MS would refuse it anyway.
            return None
        keep_until, limit, reason = offer
    cut_ms = min(keep_until + TAIL_PAD_MS, limit, audio_len)
    if audio_len - cut_ms < MIN_GAIN_MS:
        return None
    return cut_ms, reason
