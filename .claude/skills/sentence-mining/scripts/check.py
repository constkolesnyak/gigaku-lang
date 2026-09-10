#!/usr/bin/env python3
"""Prove the machine can run a mine before it downloads three and a half hours of audio.

    python3 scripts/check.py            # every check, human-readable
    python3 scripts/check.py --json     # the same as JSON

This replaced the Japanese skill's `setup.py` interview. That interview existed to make the
skill shareable; this one serves one collection, and every answer it would ask for is already
a fact of this repo (`🇩🇪 German`, `Frequency 🇩🇪`, `freq/de-yt.tsv`). What is worth checking is
not what the user wants but what is *installed*, because two of the dependencies rot on their
own: the AnkiMorphs spaCy venv is rebuilt against uv's cpython and dies whenever Anki bumps
python, and the Migaku backup is only as fresh as last night's `gigaku backup`.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import load_config, deck_main, deck_deferred          # noqa: E402
from _german import spacy_python                                    # noqa: E402

MVJ_META = os.path.expanduser("~/Library/Application Support/Anki2/addons21/MvJ Japanese/meta.json")


def anki(url, action, **params):
    body = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read())
    if resp.get("error"):
        raise RuntimeError(resp["error"])
    return resp["result"]


def checks(cfg):
    out = []

    def ok(name, good, detail=""):
        out.append({"check": name, "ok": bool(good), "detail": detail})
        return good

    # --- Anki -------------------------------------------------------------------------
    url = cfg["anki_connect_url"]
    try:
        v = anki(url, "version")
        ok("ankiconnect", True, f"version {v} at {url}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError) as e:
        ok("ankiconnect", False, f"{url}: {e} — run `bash scripts/ensure_anki.sh`")
        return out  # everything below needs the collection

    models = anki(url, "modelNames")
    if ok("note type", cfg["note_type"] in models, cfg["note_type"] or "(unset)"):
        fields = anki(url, "modelFieldNames", modelName=cfg["note_type"])
        mapped = {r: f for r, f in cfg["field_map"].items() if f}
        missing = {r: f for r, f in mapped.items() if f not in fields}
        ok("field map", not missing,
           ", ".join(f"{r}→{f}" for r, f in mapped.items()) +
           (f" | MISSING: {missing}" if missing else ""))

    decks = anki(url, "deckNames")
    for label, name in (("deck main", deck_main(cfg)), ("deck deferred", deck_deferred(cfg))):
        # Not existing yet is fine — push.py creates them. Saying so beats a scary red line.
        ok(label, True, f"{name}" + ("" if name in decks else "  (will be created on first push)"))
    dd = [d for d in cfg["dedupe_decks"] if d in decks]
    ok("dedupe decks", True, f"{dd} present of {cfg['dedupe_decks']}")

    # --- spaCy ------------------------------------------------------------------------
    py = spacy_python()
    if ok("spacy venv", bool(py), py or "no spacy-venv-python-* under Anki2/addons21"):
        model = cfg["spacy"]["model"]
        p = subprocess.run(
            [py, "-c", f"import spacy;n=spacy.load({model!r});print(spacy.__version__)"],
            capture_output=True, text=True, timeout=300)
        ok("spacy model", p.returncode == 0,
           f"{model} on spaCy {p.stdout.strip()}" if p.returncode == 0
           else (p.stderr.strip().splitlines() or ["failed"])[-1])

    # --- data ------------------------------------------------------------------------
    mig = cfg["known_words"]["migaku_csv"]
    if ok("migaku backup", os.path.exists(mig), mig):
        age = (time.time() - os.path.getmtime(mig)) / 86400
        ok("migaku freshness", age <= cfg["known_words"]["max_age_days"],
           f"{age:.1f} days old (limit {cfg['known_words']['max_age_days']})")
    for key, label in (("yt_tsv", "freq de-yt"), ("words_tsv", "freq de-words")):
        p = cfg["frequency"][key]
        ok(label, os.path.exists(p), p)

    # --- tools ------------------------------------------------------------------------
    for tool in ("yt-dlp", "ffmpeg"):
        ok(tool, bool(shutil.which(tool)), shutil.which(tool) or "not on PATH")

    # The OpenAI key is only needed by the definitions pass (step 6), which runs out of
    # freqdeck — but finding it missing after 250 cards are already in is worse than now.
    try:
        key = json.load(open(MVJ_META, encoding="utf-8"))["config"]["ai"]["openai_key"].strip()
        ok("openai key", bool(key), f"MvJ meta.json ({len(key)} chars)")
    except (OSError, KeyError, json.JSONDecodeError) as e:
        ok("openai key", False, f"{MVJ_META}: {e}")

    ok("work dir", True, cfg["work_dir"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    # --probe / --validate were the Japanese skill's two modes; there is one thing to do now.
    ap.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--validate", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    cfg = load_config()
    results = checks(cfg)
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"  {'✓' if r['ok'] else '✗'} {r['check']:18} {r['detail']}")
    bad = [r["check"] for r in results if not r["ok"]]
    if bad:
        print(f"\n{len(bad)} check(s) failed: {', '.join(bad)}", file=sys.stderr)
        return 1
    print(f"\nall {len(results)} checks passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
