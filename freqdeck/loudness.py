#!/usr/bin/env python3
"""The word recordings, brought to the loudness of everything else in the deck.

    uv run python freqdeck/loudness.py --dry     # measure and say what would change
    uv run python freqdeck/loudness.py           # normalise, in place, and re-register

Measured 2026-08-28 over the finished deck (150-file samples):

  Sentence Audio    median −23.30 LUFS   σ 0.75   (OpenAI TTS — even)
  Definition Audio  median −23.45 LUFS   σ 0.93   (OpenAI TTS — even)
  Word Audio        median −16.00 LUFS   σ 2.88   −36.3 … −9.6  (Commons: a different
                                                                 volunteer per word)

So the unevenness is entirely in the human recordings, and they also sit ~7 dB above the
synthesised speech: every card jumped from a loud word to a quiet sentence.

**The TTS is the reference and is not touched** (the user's call, 2026-08-28: match the word
recordings to the loudness of everything else) — it is two thirds of the files, it is already even,
and re-encoding it would only cost quality. The target is therefore not a constant but
**the reference kinds' own median, measured at run time**, so it stays true if the voice
or the TTS level ever changes. **A fixed gain, not a compressor**: `loudnorm`'s dynamic mode would reshape speech that is already fine, and
most of these files are under 3 s, where EBU R128's gating is not to be trusted anyway.
Each file is measured once (`ebur128`, integrated loudness and true peak), scaled by a
single `volume=<gain>dB`, and the gain is **clamped so the peak lands no higher than
−1 dBFS** — a file whose peak sits close to 0 stays a little quieter than the target
rather than clipping, and says so in the report.

Everything comes out as **mp3**, and the Commons word recordings therefore change
container: this ffmpeg has no `libvorbis` encoder (only the experimental native one), and
of the codecs it does have, mp3 is the one that plays everywhere the collection syncs —
Ogg/Opus would have been the smaller answer and is not a safe bet on iOS. A renamed file
is stored under the new name, the note's `[audio:…]` tag is rewritten to match, and
`wordaudio.tsv` follows so a rebuilt apkg cannot bring the old name back; the old file is
left in place because other German notes may still reference it.

Files are written to a temp file and swapped in, then handed to Anki through
`storeMediaFile` so its media DB knows they changed — a file edited behind Anki's back is
a file that may not sync. The twin in `out/media` is updated too. A file already within
`--tolerance` of the target and already mp3 is left completely alone.
"""
import argparse
import base64
import collections
import concurrent.futures
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import deck  # noqa: E402

ADJUST = ("Word Audio",)                            # what gets moved
REFERENCE = ("Sentence Audio", "Definition Audio")  # what it is moved to, and left alone
SAMPLE = 150          # files per reference kind — σ is under 1 dB, so this settles the median
TARGET = None         # LUFS; None = measure the reference at run time
CEILING = -1.0        # dBFS; the gain is clamped so no file peaks above this
TOLERANCE = 0.5       # dB; closer than this to the target and the file is not touched
WORKERS = 8
MEDIA = os.path.expanduser("~/Library/Application Support/Anki2/MainProfile/collection.media")
MP3 = ["-c:a", "libmp3lame", "-q:a", "4"]   # VBR ~165 kbps: past transparent for speech


def measure(path):
    """(integrated LUFS, true peak dBFS) — one ffmpeg pass, nothing written."""
    done = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af",
         "ebur128=framelog=quiet:peak=true", "-f", "null", "-"],
        capture_output=True, text=True)
    loud = re.search(r"I:\s+(-?[\d.]+) LUFS", done.stderr)
    peak = re.search(r"Peak:\s+(-?[\d.]+) dBFS", done.stderr)
    return (float(loud.group(1)) if loud else None,
            float(peak.group(1)) if peak else None)


def gain_for(loudness, peak, target, ceiling=CEILING):
    """How much to scale this file by, in dB — and never into clipping.

    A silent or unmeasurable file (loudness None, or the −70 LUFS floor ebur128 reports
    for near-silence) is left alone: there is nothing to normalise and a huge gain would
    only raise its noise."""
    if loudness is None or loudness <= -70:
        return 0.0
    want = target - loudness
    if peak is not None:
        want = min(want, ceiling - peak)
    return want


def apply_gain(src, dst, gain):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-af", f"volume={gain:.2f}dB", *MP3, dst],
                   check=True, capture_output=True)


def deck_files(deck_name=None):
    """{filename: kind} for every sound the deck's notes reference."""
    notes = deck.anki("notesInfo", notes=deck.anki(
        "findNotes", query=f'deck:"{deck_name or deck.DECK}"'))
    out = {}
    for note in notes:
        for kind in ("Word Audio", "Sentence Audio", "Definition Audio"):
            for name in re.findall(r"\[audio:(.+?)\]", note["fields"][kind]["value"]):
                out[name] = kind
    return out


def reference_level(files, workers=WORKERS, sample=SAMPLE, notetype=None):
    """The median loudness of the kinds that stay as they are — the level to match.

    Falls back to the whole note type when this deck has none of the reference kinds yet.
    That is the chicken-and-egg a fresh deck hits: with REFERENCE=("Definition Audio",) the
    target cannot be measured until definitions.py has made some, but definitions.py is what
    needs the target in order to level each word recording as it lands — so it died before
    creating anything. The level being matched is the TTS voice's, which is a property of the
    voice and not of the deck, so any 🇩🇪 note's Definition Audio answers the question.
    """
    import random
    import statistics
    picked = []
    for kind in REFERENCE:
        of_kind = [n for n, k in files.items() if k == kind]
        if not of_kind and notetype:
            wider = deck.anki("notesInfo", notes=deck.anki(
                "findNotes", query=f'note:"{notetype}" -"{kind}:"'))
            of_kind = [f for n in wider
                       for f in re.findall(r"\[audio:(.+?)\]", n["fields"][kind]["value"])]
            if of_kind:
                deck.note(f"  {kind}: none in this deck yet — measuring the {notetype} "
                          f"note type instead ({len(of_kind)} files)")
        picked += random.Random(7).sample(of_kind, min(sample, len(of_kind)))
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        measured = list(pool.map(lambda n: measure(os.path.join(MEDIA, n))[0], picked))
    values = [v for v in measured if v is not None and v > -70]
    if not values:
        raise SystemExit("could not measure the reference kinds")
    return statistics.median(values), len(values)


def process(name, kind, dry, target):
    """(kind, loudness, gain, what, new name) for one file. `new name` differs on ogg→mp3."""
    path = os.path.join(MEDIA, name)
    if not os.path.exists(path):
        return kind, None, None, "missing", name
    loudness, peak = measure(path)
    gain = gain_for(loudness, peak, target)
    stem, ext = os.path.splitext(name)
    target_name = name if ext.lower() == ".mp3" else stem + ".mp3"
    if loudness is None or loudness <= -70:
        return kind, loudness, gain, "silent", name
    if abs(gain) < TOLERANCE and target_name == name:
        return kind, loudness, gain, "already", name
    if dry:
        return kind, loudness, gain, "would", target_name
    fd, tmp = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        apply_gain(path, tmp, gain)
        os.replace(tmp, os.path.join(MEDIA, target_name))
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    # the copy the apkg is built from, where there is one
    twin = os.path.join(deck.MEDIA, target_name)
    if os.path.isdir(deck.MEDIA):
        import shutil
        shutil.copy2(os.path.join(MEDIA, target_name), twin)
    return kind, loudness, gain, "normalised", target_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--adjust", default=",".join(ADJUST),
                    help="comma-separated fields to MOVE (default %(default)r). A deck whose "
                         "sentence audio is ripped from video, not synthesised, must include "
                         "'Sentence Audio' here — measured on YouTube 🇩🇪: σ 3.58 dB over a "
                         "16 dB range, against σ 0.95 for the TTS")
    ap.add_argument("--reference", default=",".join(REFERENCE),
                    help="comma-separated fields that define the target and are left alone "
                         "(default %(default)r). Only genuinely even, synthesised audio "
                         "belongs here: ripped clips in the reference drag the median off "
                         "the TTS level everything is supposed to match")
    ap.add_argument("--deck", default=None,
                    help="normalise this deck instead of %r — the sentence-mining skill "
                         "builds 🇩🇪 notes in its own deck, whose Commons word recordings "
                         "need the same levelling against the TTS" % deck.DECK)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--tolerance", type=float, default=TOLERANCE)
    ap.add_argument("--target", type=float, default=TARGET,
                    help="LUFS to match; default is the reference kinds' measured median")
    a = ap.parse_args()
    globals()["TOLERANCE"] = a.tolerance   # `process` reads the module value
    globals()["ADJUST"] = tuple(x.strip() for x in a.adjust.split(",") if x.strip())
    globals()["REFERENCE"] = tuple(x.strip() for x in a.reference.split(",") if x.strip())
    if set(ADJUST) & set(REFERENCE):
        sys.exit(f"a field cannot be both adjusted and the reference: {set(ADJUST) & set(REFERENCE)}")

    files = deck_files(a.deck)
    target = a.target
    if target is None:
        target, n = reference_level(files, a.workers, notetype=deck.NOTETYPE)
        deck.note(f"reference ({', '.join(REFERENCE)}): {n} files measured, median {target:.2f} LUFS")
    files = {n: k for n, k in files.items() if k in ADJUST}
    deck.note(f"{len(files)} {'/'.join(ADJUST)} file(s) to match; peak ceiling {CEILING} dBFS"
              + (" (dry run)" if a.dry else ""))
    done = collections.Counter()
    changed, renamed = [], {}
    with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
        futures = {pool.submit(process, n, k, a.dry, target): n for n, k in files.items()}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            name = futures[fut]
            try:
                kind, loudness, gain, what, new_name = fut.result()
            except Exception as exc:  # noqa: BLE001 — one file's failure is one file's
                done["failed"] += 1
                deck.note(f"  {name}: FAILED {exc!r}")
                continue
            done[what] += 1
            if what in ("normalised", "would"):
                changed.append(new_name)
                if new_name != name:
                    renamed[name] = new_name
            if i % 500 == 0:
                deck.note(f"  {i}/{len(files)} …")
    deck.note(f"{dict(done)}" + (f", renamed to mp3: {len(renamed)}" if renamed else ""))
    if a.dry:
        return

    # Anki must be told: a file changed on disk behind its media DB may not sync.
    deck.note(f"re-registering {len(changed)} file(s) with Anki …")
    for i in range(0, len(changed), 50):
        actions = []
        for name in changed[i:i + 50]:
            with open(os.path.join(MEDIA, name), "rb") as f:
                actions.append({"action": "storeMediaFile",
                                "params": {"filename": name,
                                           "data": base64.b64encode(f.read()).decode()}})
        deck.anki("multi", actions=actions)

    if renamed:
        retag(renamed, a.deck)
    deck.note("done")


def retag(renamed, deck_name=None):
    """Point the deck's notes — and the caches an apkg is rebuilt from — at the new names."""
    notes = deck.anki("notesInfo", notes=deck.anki(
        "findNotes", query=f'deck:"{deck_name or deck.DECK}"'))
    touched = 0
    for note in notes:
        fields = {}
        for kind in ("Word Audio", "Sentence Audio", "Definition Audio"):
            value = note["fields"][kind]["value"]
            new_value = value
            for old, new in renamed.items():
                new_value = new_value.replace(f"[audio:{old}]", f"[audio:{new}]")
            if new_value != value:
                fields[kind] = new_value
        if fields:
            deck.anki("updateNoteFields", note={"id": note["noteId"], "fields": fields})
            touched += 1
    for name in deck.LISTS:
        deck.use_list(name)
        rows = deck.read_tsv(deck.WORDAUDIO)
        if not rows:
            continue
        with open(deck.WORDAUDIO, "w", encoding="utf-8") as f:
            for word, filename in rows:
                f.write(f"{word}\t{renamed.get(filename, filename)}\n")
    deck.use_list("words")
    deck.note(f"retagged {touched} note(s) to the new file names")


if __name__ == "__main__":
    main()
