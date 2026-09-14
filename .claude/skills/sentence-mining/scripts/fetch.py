#!/usr/bin/env python3
"""Playlist → per-episode audio + a word-timed, sentence-split German transcript.

    python3 scripts/fetch.py <playlist-or-video-url> [--limit N] [--only p03,p08]

Writes into the work dir, one pair per episode:
    pNN-<videoid>.m4a               audio only, never re-encoded
    pNN-<videoid>.transcript.json   {video_id, title, url, words[], sentences[]}

**There is no ASR step.** The Japanese skill paid AssemblyAI because Japanese has no
punctuation and its speech is hard; German on YouTube arrives already transcribed by
YouTube's own recogniser, and the `de-orig` track (the original language, not one of the
~200 machine translations of it) comes back punctuated, capitalised, and — the part that
matters — with a `tOffsetMs` per word. Measured on this playlist: 22,676 characters for a
21-minute episode, sentence punctuation good enough that no LLM splitting pass is needed.
That deletes an API key, a paid service and a whole pipeline stage.

Audio is kept as **m4a, not mp3**: `-x` would transcode three and a half hours end to end,
while ffmpeg slices the clips it actually needs straight out of the m4a later.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import load_config, work_dir  # noqa: E402

SUB_LANG = "de-orig"          # the ORIGINAL German, not `de` (a translation of it)
MIN_CHARS = 25                # shorter than this is a fragment; merge it forward
WORD_TAIL_MS = 1500           # a word never owns more than this much of a following silence

# Why an abbreviation list at all: "next token is capitalised" is a near-perfect German
# sentence-boundary test, since German capitalises sentence starts AND every noun — so the
# usual false positives don't arise. These are what is left, and the playlist supplies the
# motivating case itself: "Dr. Stone" split into "…dass Dr." + "Stone letz Zeit…" until
# `Dr` went in here.
ANNOTATION = re.compile(r"^\[.*\]$")

ABBREV = {"dr", "prof", "nr", "st", "hr", "fr", "mr", "mrs", "ca", "bzw", "usw", "etc",
          "ggf", "inkl", "evtl", "z", "b", "u", "a", "d", "h", "vgl", "s", "sog", "max", "min"}


def note(msg):
    print(msg, file=sys.stderr, flush=True)


def yt(*args, parse=False):
    p = subprocess.run(["yt-dlp", *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"yt-dlp {args[-1]}: {p.stderr.strip().splitlines()[-1:] or p.stderr}")
    return json.loads(p.stdout) if parse else p.stdout


def entries(url, limit):
    """The playable videos of a playlist, in order. Unavailable ones carry no duration."""
    data = yt("--flat-playlist", "-J", url, parse=True)
    if data.get("_type") != "playlist":
        return [{"id": data["id"], "title": data.get("title", ""),
                 "duration": data.get("duration"),
                 "channel": (data.get("channel") or data.get("uploader") or "").strip()}]
    out = []
    for e in data.get("entries", []):
        if not e.get("id"):
            continue
        if e.get("duration") is None:
            note(f"  skipping unavailable entry {e['id']}")
            continue
        out.append({"id": e["id"], "title": e.get("title", ""), "duration": e.get("duration"),
                    "channel": (e.get("channel") or e.get("uploader")
                                or data.get("channel") or data.get("uploader") or "").strip()})
    return out[:limit] if limit else out


def download(stem, video_id, wd):
    """Audio + captions for one video. Idempotent — an existing pair is left alone."""
    audio = next((p for p in (wd / f"{stem}.m4a", wd / f"{stem}.webm", wd / f"{stem}.mp4")
                  if p.exists()), None)
    subs = wd / f"{stem}.{SUB_LANG}.json3"
    if audio and subs.exists():
        note(f"  {stem}: already downloaded")
        return audio, subs
    yt("-f", "bestaudio[ext=m4a]/bestaudio",
       "--write-auto-subs", "--sub-langs", SUB_LANG, "--sub-format", "json3",
       "--no-playlist", "--no-progress", "-o", str(wd / f"{stem}.%(ext)s"),
       f"https://www.youtube.com/watch?v={video_id}")
    audio = next((p for p in wd.glob(f"{stem}.*")
                  if p.suffix in (".m4a", ".webm", ".mp4", ".opus")), None)
    if not audio:
        raise RuntimeError(f"{stem}: yt-dlp wrote no audio")
    if not subs.exists():
        raise RuntimeError(f"{stem}: no {SUB_LANG} captions — is the video German?")
    return audio, subs


def words_from_json3(path):
    """Flatten YouTube's caption events into one word list with real start/end times.

    `aAppend` events are the roll-up repeat of text already emitted — taking them would
    duplicate every line. A word's end is the next word's start, capped so a word before a
    long silence does not swallow it, and the last word of an event is bounded by that
    event's own declared duration.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    words = []
    for e in data.get("events", []):
        if e.get("aAppend"):
            continue
        base, dur = e.get("tStartMs", 0), e.get("dDurationMs", 0)
        # `[schnauben]`, `[Musik]`, `[ __ ]` (YouTube's profanity mask) are annotations the
        # recogniser adds, not words anyone said. Rare — 8 in 2,313 sentences on this
        # playlist — but one of them became a card for `schnauben`, whose clip is a snort.
        segs = [s for s in e.get("segs", [])
                if s.get("utf8", "").strip() and not ANNOTATION.match(s["utf8"].strip())]
        for i, s in enumerate(segs):
            start = base + s.get("tOffsetMs", 0)
            nxt = segs[i + 1].get("tOffsetMs") if i + 1 < len(segs) else None
            hard_end = base + nxt if nxt is not None else base + dur
            words.append({"text": s["utf8"].strip(), "start_ms": start, "end_ms": max(hard_end, start + 60)})
    words.sort(key=lambda w: w["start_ms"])
    for i, w in enumerate(words[:-1]):
        w["end_ms"] = min(max(w["end_ms"], w["start_ms"] + 60),
                          words[i + 1]["start_ms"], w["start_ms"] + WORD_TAIL_MS)
    return words


def ends_sentence(word_text, next_text):
    """Does a sentence end after this word? German capitalisation carries the decision."""
    if not re.search(r"[.!?…]+[\"'»）)]?$", word_text):
        return False
    stem = re.sub(r"[^\wäöüßÄÖÜ]", "", word_text, flags=re.UNICODE).lower()
    if stem in ABBREV or len(stem) <= 1:
        return False
    if next_text and not next_text[0].isupper():
        return False   # German starts sentences upper-case; lower-case means we are mid-sentence
    return True


def split_sentences(words):
    """Word list → card-sized chunks, each carrying the slice of words it was built from."""
    chunks, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1]["text"] if i + 1 < len(words) else ""
        if ends_sentence(w["text"], nxt):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)

    # Merge anything too short to be a sentence into its neighbour. A fragment makes a
    # broken-sounding clip, which the Japanese skill's curation notes call out explicitly.
    merged = []
    for c in chunks:
        text = " ".join(w["text"] for w in c)
        if merged and len(text) < MIN_CHARS:
            merged[-1].extend(c)
        else:
            merged.append(c)

    out = []
    for idx, c in enumerate(merged):
        out.append({
            "idx": idx,
            "text": re.sub(r"\s+", " ", " ".join(w["text"] for w in c)).strip(),
            "start_ms": c[0]["start_ms"],
            "end_ms": c[-1]["end_ms"],
            "words": c,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma-separated stems, e.g. p03,p08")
    a = ap.parse_args()

    cfg = load_config()
    wd = work_dir(cfg)
    only = {s.strip() for s in a.only.split(",") if s.strip()}

    eps = entries(a.url, a.limit)
    note(f"{len(eps)} playable entries, {sum(e['duration'] for e in eps) / 3600:.2f}h total")

    manifest = []
    for i, e in enumerate(eps, 1):
        stem = f"p{i:02d}-{e['id']}"
        if only and stem.split("-")[0] not in only and stem not in only:
            continue
        note(f"[{i}/{len(eps)}] {stem}  {e['title'][:60]}")
        audio, subs = download(stem, e["id"], wd)
        words = words_from_json3(subs)
        sentences = split_sentences(words)
        tpath = wd / f"{stem}.transcript.json"
        tpath.write_text(json.dumps({
            "stem": stem,
            "video_id": e["id"],
            "title": e["title"],
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "audio": str(audio),
            "duration_s": e["duration"],
            "words": words,
            "sentences": sentences,
        }, ensure_ascii=False), encoding="utf-8")
        lens = [len(s["text"]) for s in sentences]
        note(f"    {len(words)} words, {len(sentences)} sentences, "
             f"median {sorted(lens)[len(lens) // 2] if lens else 0} chars → {tpath.name}")
        manifest.append({"stem": stem, "transcript": str(tpath), "audio": str(audio),
                         "video_id": e["id"], "title": e["title"],
                         "channel": e.get("channel", ""),
                         "url": f"https://www.youtube.com/watch?v={e['id']}"})

    mpath = wd / "manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"\nwrote {mpath} ({len(manifest)} episodes)")


if __name__ == "__main__":
    main()
