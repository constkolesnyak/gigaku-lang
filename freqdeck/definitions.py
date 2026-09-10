#!/usr/bin/env python3
"""Definitions, definition audio and word audio for Frequency 🇩🇪 notes — by script, not UI.

    uv run python freqdeck/definitions.py            # every note in the deck missing something
    uv run python freqdeck/definitions.py --dry      # only say what would be done

Does for a note exactly what the MvJ 💣 does for a German note, with the same inputs and
the same outputs, but over AnkiConnect from outside Anki (the user's question, 2026-08-27:
why go through the Anki UI at all? — the UI path cost modal dialogs, a blocked main thread
and Commons 429s inside the add-on; this costs nothing but API calls):

- Definition   — gpt-4o (the add-on's `ai` block: model, temperature 0.7, max_tokens 512,
                 top_p, penalties) with `german_prompts.definition_prompt` +
                 `definition_system_prompt` from the add-on's meta.json, context="" —
                 formatted with str.format exactly as mvj/services/ai.py does — written as
                 the add-on's `<!-- def-type="bilingual" -->…<!-- def-end -->` block.
                 English only, by the user's decision: the monolingual block is never made.
- Definition Audio — OpenAI tts-1/shimmer of that text (the add-on's english_tts), stored as
                 mvj-bilingual-definition-<ms>.mp3, wrapped with the TTS-SOURCE comment.
- Word Audio   — only when EMPTY or pointing at a file that is not there: one Commons
                 lookup for De-<Wort>.ogg (paced — the add-on's probe is what 429s), else
                 TTS of the word as <wort>_mvj-word-tts-<ms>.mp3 (the add-on's fallback name).

Every note is finished independently and written the moment its pieces exist, so a
failure costs one note; re-running picks up whatever is still missing. Four workers: the
OpenAI calls dominate and parallelise; AnkiConnect writes are serialised by a lock.
"""
import argparse
import base64
import concurrent.futures
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import deck  # noqa: E402  (tts, media names, Commons helpers, META)
import loudness  # noqa: E402  (measure/gain/transcode — levelling happens per file, not in a pass)

DECK = deck.DECK        # overridden by --deck; see main()
WORKERS = 4
_lock = threading.Lock()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def anki(action, **params):
    with _lock:
        return deck.anki(action, **params)


def config():
    meta = json.load(open(deck.META, encoding="utf-8"))["config"]
    ai = meta["ai"]
    gp = meta["german_prompts"]
    return {
        "key": ai["openai_key"].strip(),
        "model": ai.get("openai_model") or "gpt-4o",
        "temperature": ai.get("openai_temperature", 0.7),
        "max_tokens": ai.get("openai_max_tokens", 512),
        "top_p": ai.get("openai_top_p"),
        "frequency_penalty": ai.get("openai_frequency_penalty"),
        "presence_penalty": ai.get("openai_presence_penalty"),
        "prompt": gp["definition_prompt"],
        "system": gp.get("definition_system_prompt") or "",
    }


def chat(cfg, system, user):
    """mvj/services/ai.py::OpenAIClient.generate_text for a gpt-4o-class model."""
    payload = {"model": cfg["model"],
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "temperature": cfg["temperature"], "max_tokens": cfg["max_tokens"]}
    for k in ("top_p", "frequency_penalty", "presence_penalty"):
        if cfg[k] is not None:
            payload[k] = cfg[k]
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {cfg['key']}",
                                          "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(5 * (attempt + 1))


def store(name, data):
    return anki("storeMediaFile", filename=name, data=base64.b64encode(data).decode())


def definition_block(text):
    return f'<!-- def-type="bilingual" -->\n{text}\n<!-- def-end -->'


def audio_block(text, stored):
    return (f'<!-- def-type="bilingual" TTS-SOURCE: {text.replace("-->", "- ->")} -->\n'
            f'[audio:{stored}]\n<!-- def-end -->')


def bilingual_text(field):
    m = re.search(r'<!-- def-type="bilingual" -->\s*(.*?)\s*<!-- def-end -->', field, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def level(stored, target):
    """Bring one just-downloaded word recording to the deck's TTS level, immediately.

    Levelling used to be a whole separate pass over the finished deck, which is exactly how
    it gets forgotten — it can only run after a word-audio pass that takes hours, so by the
    time it is possible nobody is looking. Doing it here makes it part of downloading the
    file: a recording is never in the collection at the wrong level, not even briefly.

    A Commons file also changes container here (ogg → mp3; this ffmpeg has no libvorbis), so
    the name changes and the caller must write back the name this returns, not the one it
    passed in. The orphaned ogg is removed — we downloaded it seconds ago, so nothing else
    can be pointing at it.
    """
    if target is None:
        return stored
    _, _, _, what, new_name = loudness.process(stored, "Word Audio", False, target)
    if what != "normalised":
        return stored
    with open(os.path.join(loudness.MEDIA, new_name), "rb") as fh:
        store(new_name, fh.read())
    if new_name != stored:
        try:
            anki("deleteMediaFile", filename=stored)
        except Exception:  # noqa: BLE001 — an orphan left behind is not worth failing a card
            pass
    return new_name


def commons_word_audio(word):
    """(stored file name, source) — a Commons recording if one exists, else TTS."""
    cands = [word, word[0].swapcase() + word[1:]]
    found = deck.commons_lookup(cands)
    cand = next((c for c in cands if c in found), None)
    if cand:
        data = deck._commons(found[cand].split("?")[0])
        time.sleep(deck.DOWNLOAD_PACE)
        if data.startswith(b"OggS"):
            return store(f"de-{cand}.ogg".lower(), data), "commons"
    data = deck.tts(word, config()["key"])
    return store(f"{word}_mvj-word-tts-{int(time.time() * 1000)}.mp3", data), "tts"


def finish(note, cfg, present, dry, word_audio=True, target=None):
    f = lambda k: note["fields"][k]["value"]
    word, sentence = f("Word").strip(), f("Sentence").strip()
    fields, done = {}, []
    if not f("Definition").strip():
        if dry:
            done.append("definition")
        else:
            text = chat(cfg, cfg["system"],
                        cfg["prompt"].format(word=word, sentence=sentence, context=""))
            text = text.strip().strip('"').strip()
            fields["Definition"] = definition_block(text)
            done.append("definition")
    text = bilingual_text(fields.get("Definition", f("Definition")))
    if text and not f("Definition Audio").strip():
        if not dry:
            data = deck.tts(text, cfg["key"])
            stored = store(f"mvj-bilingual-definition-{int(time.time() * 1000)}.mp3", data)
            fields["Definition Audio"] = audio_block(text, stored)
        done.append("definition audio")
    wa = f("Word Audio")
    tags = re.findall(r"\[audio:(.+?)\]", wa)
    if word_audio and (not wa.strip() or any(t not in present for t in tags)):
        if not dry:
            stored, source = commons_word_audio(word)
            stored = level(stored, target)
            fields["Word Audio"] = f"[audio:{stored}]"
            done.append(f"word audio ({source})")
        else:
            done.append("word audio")
    if fields and not dry:
        anki("updateNoteFields", note={"id": note["noteId"], "fields": fields})
    return word, done


def main():
    global DECK          # declared before the parser, which reads DECK for its default
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--deck", default=DECK,
                    help="finish the notes of this deck instead of %(default)r — the "
                         "sentence-mining skill builds 🇩🇪 notes in its own deck and needs "
                         "the identical 💣 treatment so both decks read and sound alike")
    ap.add_argument("--no-word-audio", action="store_true",
                    help="skip the Word Audio pass. Commons limits by COUNT (~60 files/hour, "
                         "then a 600s ban), so it is hours for a big deck while definitions "
                         "take minutes — this makes the deck usable first, exactly the split "
                         "deck.py already makes by running `wordaudio` as its own stage")
    ap.add_argument("--reference", default=",".join(loudness.REFERENCE),
                    help="fields that define the loudness target for the inline levelling "
                         "(default %(default)r). A deck whose Sentence Audio is ripped from "
                         "video must NOT list it here — it is uneven by construction (σ 3.58 "
                         "dB measured) and drags the median off the TTS level")
    ap.add_argument("--skip-suspended", action="store_true",
                    help="enrich only cards that are actually being studied. Definitions, "
                         "TTS and especially Commons word audio are the expensive part of a "
                         "build, and a suspended card is one that was already judged not "
                         "worth teaching — paying for it is pure waste (measured: 20 word "
                         "recordings downloaded for cards that were already suspended)")
    ap.add_argument("--no-normalize", action="store_true",
                    help="do NOT level each word recording against this deck's TTS as it "
                         "lands. On by default: Commons volunteers sit ~7 dB above "
                         "tts-1/shimmer, and a levelling pass that can only run after a "
                         "multi-hour download is a pass that gets skipped")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--redo", help="comma-separated words whose Definition is cleared first — "
                                   "use it when their SENTENCE changed, since the definition "
                                   "is derived from it (measured: stürzen went from falling "
                                   "over to overthrowing a king, the old gloss stayed)")
    a = ap.parse_args()
    DECK = a.deck
    cfg = config()
    media = os.path.expanduser("~/Library/Application Support/Anki2/MainProfile/collection.media")
    present = set(os.listdir(media))
    query = f'deck:"{DECK}"' + (" -is:suspended" if a.skip_suspended else "")
    notes = anki("notesInfo", notes=anki("findNotes", query=query))
    f = lambda n, k: n["fields"][k]["value"]
    if a.redo:
        wanted = {w.strip() for w in a.redo.split(",") if w.strip()}
        stale = [n for n in notes if f(n, "Word").strip() in wanted
                 and (f(n, "Definition").strip() or f(n, "Definition Audio").strip())]
        log(f"clearing {len(stale)} definition(s) whose sentence changed")
        for n in stale:
            anki("updateNoteFields", note={"id": n["noteId"],
                                           "fields": {"Definition": "", "Definition Audio": ""}})
        notes = anki("notesInfo", notes=anki("findNotes", query=f'deck:"{DECK}"'))
    loudness.REFERENCE = tuple(x.strip() for x in a.reference.split(",") if x.strip())
    target = None
    if not a.no_word_audio and not a.no_normalize and not a.dry:
        # Measured once per run: the target is this deck's own Sentence/Definition Audio
        # median, so it stays true if the TTS voice or level ever changes.
        target, seen = loudness.reference_level(loudness.deck_files(DECK), a.workers,
                                                notetype=deck.NOTETYPE)
        log(f"levelling word audio to {target:.2f} LUFS (median of {seen} TTS files)")

    todo = [n for n in notes if not f(n, "Definition").strip() or not f(n, "Definition Audio").strip()
            or (not a.no_word_audio
                and (not f(n, "Word Audio").strip()
                     or any(t not in present
                            for t in re.findall(r"\[audio:(.+?)\]", f(n, "Word Audio")))))]
    log(f"{len(notes)} notes in {query}, {len(todo)} need work"
        + (" (dry run)" if a.dry else f", {a.workers} workers, model {cfg['model']}"))
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
        futures = {pool.submit(finish, n, cfg, present, a.dry, not a.no_word_audio, target): n
                   for n in todo}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                word, done = fut.result()
                log(f"  {i}/{len(todo)} {word}: {', '.join(done) or 'nothing'}")
            except Exception as e:  # one note's failure is one note's failure
                failed += 1
                log(f"  {i}/{len(todo)} {futures[fut]['fields']['Word']['value']}: FAILED {e!r}")
    log(f"done, {failed} failed")


if __name__ == "__main__":
    main()
