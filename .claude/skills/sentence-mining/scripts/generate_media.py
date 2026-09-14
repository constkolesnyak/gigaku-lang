#!/usr/bin/env python3
"""Cut each candidate's sentence out of the episode audio and register it with Anki.

    python3 scripts/generate_media.py            # every candidate missing a clip
    python3 scripts/generate_media.py --limit 5  # try a handful first

Writes `sentence_audio_file` onto each candidate and leaves `draft.json` for `push.py`.

Only ONE piece of media is made here, and that is deliberate: the card's sentence audio is
the speaker's actual voice, cut from the m4a with ffmpeg. There is no screenshot (asked for:
audio only), and no TTS — the Japanese skill synthesised its explanation audio because the
explanation was written from scratch, whereas here the Definition and its audio are made
later by `freqdeck/definitions.py`, which is what already voices `Frequency 🇩🇪` and so keeps
the two decks sounding like one collection.
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "freqdeck"))
from _config import load_config, work_dir           # noqa: E402
from _anki import anki_request                      # noqa: E402
import deck as freqdeck                             # noqa: E402

MAX_CLIP_MS = 10000   # hard ceiling on a card's length
PAD_MS = 180          # fallback only, when no word boundaries arrived
WORKERS = 4
MIN_MP3 = 2000      # freqdeck's rule: anything shorter is a truncated file, not speech


def note(msg):
    print(msg, file=sys.stderr, flush=True)


def media_name(cfg, candidate, idx):
    """`sm_de_p01_007_praemisse.mp3` — lowercase ASCII, and that is measured.

    Anki's media store lowercased every `freq_de_001_Macht.mp3` on the way in while the note
    kept the capital: silent on this case-insensitive disk, a missing file after a sync.
    `ascii_slug` is freqdeck's, not a second copy of the same rule.
    """
    part = candidate["stem"].split("-")[0]
    return f"{cfg['media_prefix']}_{part}_{idx:03d}_{freqdeck.ascii_slug(candidate['lemma'])}.mp3"


SEARCH_MS = 450          # how far from the phrase boundary a quiet spot is looked for
MARGIN_MS = 700          # the margin cut out around the phrase before searching
MIN_WORD_MS = 150        # the last word is certainly still sounding this long after its start
ANCHOR_SLACK = 80        # how far past the first word's start the cut may land
FRAME_MS = 10            # envelope step
WIN_MS = 60              # the window whose quietness is scored


def _envelope(path):
    """RMS per FRAME_MS frame over a mono copy of the piece.

    Computed by hand on `array` rather than through audioop, which was removed from Python
    in 3.13. At 16 kHz mono this is tens of thousands of samples — the cost is negligible.
    """
    import array
    import wave
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    pcm = array.array("h")
    pcm.frombytes(raw[: len(raw) // 2 * 2])
    step = max(1, int(rate * FRAME_MS / 1000))
    out = []
    for k in range(0, len(pcm) - step, step):
        acc = 0
        for v in pcm[k:k + step]:
            acc += v * v
        out.append((acc / step) ** 0.5 or 1.0)
    return out


# Strict rungs only. A loose −26dB used to be in the ladder and took a merely quieter spot
# inside speech for a pause: 5 bad endings out of the 10 it fired on, against 2 of 42 at −42dB.
SILENCE_LADDER = ("-42dB", "-36dB", "-30dB")
SILENCE_MIN = 0.09       # 90 ms: anything shorter is a stop inside a word, not a pause
END_REACH_MS = 900       # the end is searched twice as far as the start: the pause after a
                         # phrase can be delayed, and 450 ms does not see it (measured: end
                         # −53.7 dB against −49.6, 14 bad against 16)
GUARD_MS = 15
FADE_MS = 25             # where there is physically no pause, the cut is audible as a click
# The right-hand limit is the start of the next phrase PLUS this slack: YouTube's timings
# place a word earlier than it sounds, and a limit without slack shut the search out just
# before the pause. Measured over 94 clips (slack → bad endings / foreign word in the
# tail / phrase intact):
#   0 → 23 / 3 / 91,  150 → 19 / 3 / 93,  300 → 16 / 4 / 93,  450 → 14 / 4 / 93,  ∞ → 14 / 4 / 93.
# Which also shows that "29 clips run into the next word" was an artefact of the timings:
# acoustically a foreign tail is audible in three or four, almost regardless of the limit.
END_LIMIT_SLACK_MS = 450


def _silences(path, noise):
    """[(start, end)] of the silences in the piece, in milliseconds."""
    p = subprocess.run(["ffmpeg", "-i", path, "-af",
                        f"silencedetect=noise={noise}:d={SILENCE_MIN}", "-f", "null", "-"],
                       capture_output=True, text=True)
    a = [float(x) * 1000 for x in re.findall(r"silence_start: ([\d.]+)", p.stderr)]
    b = [float(x) * 1000 for x in re.findall(r"silence_end: ([\d.]+)", p.stderr)]
    return list(zip(a, b))


def _pause_after(anchor_ms, sils, limit_ms):
    """The first pause after the last word and strictly before `limit_ms`, as (start, end)."""
    best = None
    for a, b in sils:
        if b - a < 2 * GUARD_MS or a < anchor_ms + MIN_WORD_MS:
            continue
        if a > limit_ms:
            continue
        if best is None or a < best[0]:
            best = (a, b)
    return best


def _quietest(env, lo_ms, hi_ms, at="end"):
    """The quietest window in the range; the cut is at its end (`at="end"`) or its start.

    For the START this beats a threshold pause search: a threshold is binary, it fires on
    the first "quiet enough" spot, and there is usually a quieter one before a phrase.
    Measured over all 94: the chosen point matches the quietest reachable one, 0.0 dB lost.
    """
    n = WIN_MS // FRAME_MS
    lo = max(0, int(lo_ms // FRAME_MS))
    hi = min(len(env) - n, int(hi_ms // FRAME_MS) - n)
    if hi < lo:
        return None
    k = min(range(lo, hi + 1), key=lambda i: _win(env, i, n))
    return (k + n) * FRAME_MS if at == "end" else k * FRAME_MS


def _win(env, k, n):
    return sum(env[k:k + n]) / max(1, len(env[k:k + n]))


def clip(audio, start_ms, end_ms, out_path, words=None, sent=None):
    """Cut the phrase out, placing the edges in the pauses around its FIRST and LAST word.

    Returns (start, end) in ms of the source.

    The piece is taken with a MARGIN_MS margin — searching for silence across a whole
    twenty-minute video would cost many times more, and everything needed sits at the
    boundary. The result is encoded from that same piece with no format downgrade: it was
    once cut at 16 kHz "for the detector", and every card's audio went through 16 kHz.
    """
    lo = max(0, start_ms - MARGIN_MS)
    hi = end_ms + MARGIN_MS
    piece = out_path + ".piece.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{lo / 1000:.3f}",
                    "-i", audio, "-t", f"{(hi - lo) / 1000:.3f}",
                    "-vn", "-c:a", "pcm_s16le", piece], check=True)

    # Anchors are taken from the PHRASE's boundaries, not the clip's: the clip carries a
    # margin, and that margin pulls the next word in. That is exactly how `Hauptcharakter`
    # took "Diese" — the first word of the NEXT phrase — as its anchor and was cut off on it.
    s0, s1 = sent if sent else (start_ms, end_ms)
    inside = [w for w in (words or []) if s0 <= w["start_ms"] < s1]
    a_anchor = (inside[0]["start_ms"] if inside else start_ms) - lo
    b_anchor = (inside[-1]["start_ms"] if inside else end_ms) - lo

    # The search's right-hand limit is the start of the FIRST WORD OF THE NEXT PHRASE. Without
    # it the pause search ran past it and brought back the tail of a foreign phrase: 29 clips
    # of 94, 23 of them deeper than 100 ms, audible as a stray "Und…" at the end of a card.
    after = [w for w in (words or []) if w["start_ms"] >= s1]
    b_limit = min(b_anchor + MIN_WORD_MS + END_REACH_MS,
                  (after[0]["start_ms"] - lo + END_LIMIT_SLACK_MS) if after else 10 ** 9)

    # The envelope is computed on a cheap mono copy; the encode comes from the full one.
    # 16 kHz, not 8: at 8 the envelope sees nothing above 4 kHz, i.e. exactly the sibilants
    # (measured: 27 bad starts against 28, and 1 dB quieter).
    probe = out_path + ".probe.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", piece,
                    "-ac", "1", "-ar", "16000", probe], check=True)
    env = _envelope(probe)
    os.remove(probe)

    # Each edge has its own rule, and that is not taste but a measurement over all 94 clips
    # with one metric: the mean level of the 60 ms of source right PAST the cut, against
    # ~−34 dB of speech.
    #
    #                                start             end        phrase intact
    #   energy minimum on both       −51.4 (7 bad)     −39.0 (42)     87/94
    #   pause search on both         −42.4 (28)        −49.6 (15)     94/94
    #   threshold off phrase level   −36.0 (61)        −47.0 (19)     80/94
    #   as done here                 −44.2 (27)        −53.7 (14)     93/94
    #
    # Start — ENERGY MINIMUM: the chosen point matches the quietest reachable one on all 94
    # (0.0 dB lost), so there is nothing better to look for — the remaining 27 are places
    # where the previous phrase runs straight on and there is physically no pause.
    # End — PAUSE SEARCH: the energy minimum there found a stop INSIDE the last word and
    # ate it (42 bad endings, 7 lost tails).
    a = _quietest(env, a_anchor - SEARCH_MS, a_anchor + ANCHOR_SLACK)
    b = None
    for noise in SILENCE_LADDER:
        pause = _pause_after(b_anchor, _silences(piece, noise), b_limit)
        if pause is None:
            continue
        # Cut at the VERY START of the pause, not in its quiet middle. The envelope said the
        # opposite (the middle 4.5 dB quieter), and that was its blind spot: the probe hears
        # up to 4 kHz, and above that the next word's sibilant rises inside the pause. A
        # full-band measurement: the pause's edge −49.6 dB against −44.3 for the middle,
        # 15 bad endings against 23.
        b = pause[0] + GUARD_MS
        break
    if b is None:
        # No pause on any rung — the speech runs straight on. Then the quietest spot beats
        # the transcript's boundary, which sits exactly on the start of the next word
        # (measured: all 8 such clips were cut mid-speech, median −30.6 dB).
        b = _quietest(env, b_anchor + MIN_WORD_MS, b_limit, at="start")
    a = s0 - lo if a is None else a
    b = s1 - lo if b is None else b
    if b <= a:
        a, b = start_ms - lo, end_ms - lo
    if b - a > MAX_CLIP_MS:
        b = a + MAX_CLIP_MS
    dur = (b - a) / 1000
    fade = FADE_MS / 1000
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{a / 1000:.3f}",
                    "-i", piece, "-t", f"{dur:.3f}",
                    "-af", f"afade=t=in:st=0:d={fade},afade=t=out:st={dur - fade:.3f}:d={fade}",
                    "-acodec", "libmp3lame", "-q:a", "4", out_path], check=True)
    os.remove(piece)
    return lo + a, lo + b        # the chosen bounds in ms of the source — so a test rig
                                 # measures this very code, not its own copy of the logic


def one(cfg, candidate, idx, tmp, words=None):
    name = media_name(cfg, candidate, idx)
    local = os.path.join(tmp, name)
    bounds = clip(candidate["audio"],
         candidate.get("clip_start_ms", candidate["sentence_start_ms"] - PAD_MS),
         candidate.get("clip_end_ms", candidate["sentence_end_ms"] + PAD_MS), local,
         words, (candidate["sentence_start_ms"], candidate["sentence_end_ms"]))
    candidate["cut_start_ms"], candidate["cut_end_ms"] = bounds
    size = os.path.getsize(local)
    if size < MIN_MP3:
        raise RuntimeError(f"{name}: {size} bytes — the clip came out empty")
    candidate["sentence_audio_file"] = name
    return name, size, local


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--recut", action="store_true",
                    help="re-cut even the candidates that already have a clip (same file names)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    a = ap.parse_args()

    cfg = load_config()
    wd = work_dir(cfg)
    data = json.loads((wd / "chosen.json").read_text(encoding="utf-8"))
    todo = [(i, c) for i, c in enumerate(data["candidates"])
            if a.recut or not c.get("sentence_audio_file")]
    if a.limit:
        todo = todo[:a.limit]
    # Word timings are needed to place the cut in the pause around the PHRASE's first and
    # last word: subtitle boundaries will not do, their end is the start of the next word.
    words = {}
    manifest = wd / "manifest.json"
    if manifest.exists():
        for m in json.loads(manifest.read_text(encoding="utf-8")):
            try:
                words[m["stem"]] = json.loads(
                    open(m["transcript"], encoding="utf-8").read())["words"]
            except (OSError, KeyError, json.JSONDecodeError):
                pass
    note(f"{len(data['candidates'])} candidates, {len(todo)} need a clip, {a.workers} workers")

    failed, pending = [], []
    with tempfile.TemporaryDirectory(prefix="sm_de_") as tmp:
        with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
            futs = {pool.submit(one, cfg, c, i, tmp, words.get(c["stem"])): c
                    for i, c in todo}
            for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                c = futs[fut]
                try:
                    name, size, local = fut.result()
                    pending.append((name, local))
                    if n % 25 == 0 or n == len(todo):
                        note(f"  {n}/{len(todo)}  {c['lemma']} → {name} ({size // 1024}kB)")
                except Exception as e:            # one clip's failure is one card's failure
                    failed.append(c["lemma"])
                    note(f"  {n}/{len(todo)}  {c['lemma']}: FAILED {e!r}")

        # Inside the `with`, not after it: the paths point into this very temp directory,
        # and a batched store placed after its close would be saving already-deleted files.
        # Batched, not per file: Anki raises its progress panel on every storeMediaFile,
        # and 94 separate stores are 94 flickers over someone else's work.
        for i in range(0, len(pending), 25):
            anki_request("multi", actions=[
                {"action": "storeMediaFile",
                 "params": {"filename": n, "path": os.path.abspath(l)}}
                for n, l in pending[i:i + 25]])
        note(f"  media stored in batches: {len(pending)}")

    draft = wd / "draft.json"
    draft.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ready = sum(1 for c in data["candidates"] if c.get("sentence_audio_file"))
    note(f"\n  {ready} candidates have audio"
         + (f", {len(failed)} failed: {failed}" if failed else "")
         + f"\n  wrote {draft}")


if __name__ == "__main__":
    main()
