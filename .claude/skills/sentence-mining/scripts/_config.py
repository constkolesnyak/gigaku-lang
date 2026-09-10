"""Load the skill's config.json into a dict, merged over sane defaults.

config.json sits at the skill root and is the ONE place that knows the note type, the
field mapping, the deck names, where the Migaku truth-set lives and where the frequency
lists live. Every script imports `load_config()` so no name is spelled twice.

Unlike the Japanese original there is no setup interview: this skill serves one
collection, and the answers the interview would ask for are already facts of this repo
(`🇩🇪 German`, `Frequency 🇩🇪`, `freq/de-yt.tsv`). `check.py --validate` is what replaced it.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = SKILL_ROOT.parents[2]   # .claude/skills/<skill> → the gigaku checkout
CONFIG_PATH = SKILL_ROOT / "config.json"

# Every value under these keys is a filesystem path: ~ is expanded on load and a relative
# path is taken from the repo root, so the config file stays readable and no script has
# to remember to expanduser().
_PATH_KEYS = {"work_dir"}
_NESTED_PATH_KEYS = {
    "known_words": {"migaku_csv", "gigaku_cache"},
    "frequency": {"yt_tsv", "words_tsv"},
    "spacy": {"lemma_cache"},
}

DEFAULTS: dict = {
    "anki_connect_url": "http://127.0.0.1:8765",
    "note_type": "",
    # Internal role -> the note type's actual field name. Only mapped roles are written,
    # so a field left blank here is simply never touched (that is how `Image` stays empty).
    "field_map": {"word": "", "sentence": "", "sentence_audio": "", "source": ""},
    "decks": {"parent": "", "main": "", "deferred": ""},
    # Decks whose `Word` values already exist and must not be minted twice. Deliberately
    # NOT the 46k-card sentence-bank deck — that is reference
    # material, not cards the user studies, so a word appearing there is not a duplicate.
    "dedupe_decks": [],
    "known_words": {
        # The single source of truth for "known": Migaku, and nothing else. Written daily
        # by `gigaku backup`. See references/known-words.md for why not the word cache.
        "migaku_csv": "",
        "gigaku_cache": "",   # a second witness only — never a known-set of its own
        "max_age_days": 14,
    },
    "frequency": {
        "yt_tsv": "",       # rank/pm/ru over spoken YouTube German — the grading gate
        "words_tsv": "",    # the broader list — membership only, it is not rank-ordered
        "grade_gate": 1500,
    },
    "spacy": {"model": "de_core_news_md", "lemma_cache": ""},
    "media_prefix": "sm_de",
    "tags": [],
    "cap": 400,
    "work_dir": "",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _path(value: str) -> str:
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else REPO_ROOT / p)


def _expand_paths(cfg: dict) -> dict:
    for k in _PATH_KEYS:
        if cfg.get(k):
            cfg[k] = _path(cfg[k])
    for parent, keys in _NESTED_PATH_KEYS.items():
        for k in keys:
            if cfg.get(parent, {}).get(k):
                cfg[parent][k] = _path(cfg[parent][k])
    return cfg


def load_config(required: bool = True) -> dict | None:
    if not CONFIG_PATH.exists():
        if required:
            raise SystemExit(f"No config.json at {CONFIG_PATH}")
        return None
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _expand_paths(_deep_merge(DEFAULTS, raw))


def channel(cfg: dict) -> str:
    """The channel these episodes came from, as `fetch.py` recorded it in the manifest."""
    m = Path(cfg["work_dir"] or "") / "manifest.json"
    if not m.exists():
        return ""
    try:
        entries = json.loads(m.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    for e in entries:
        if e.get("channel"):
            return re.sub(r'["*:<>?/\\|]', "-", e["channel"]).strip()   # Anki deck names
    return ""


def deck_main(cfg: dict) -> str:
    """`<parent>::<channel>` — one deck per channel under one YouTube parent.

    A channel is the unit here: its vocabulary, its speaker, its subject, and the frequency
    the deck is ordered by is counted inside it. Keeping them in one flat deck would mix
    those, and a per-channel new-card limit is the thing you actually want to set.
    `decks.main` remains the fallback for a run with no manifest yet.
    """
    parent, ch = cfg["decks"].get("parent", ""), channel(cfg)
    if parent and ch:
        return f"{parent}::{ch}"
    return cfg["decks"].get("main", "") or ""


def deck_deferred(cfg: dict) -> str:
    """Deferred deck, falling back to the main deck when unset."""
    return cfg["decks"].get("deferred", "") or deck_main(cfg)


def work_dir(cfg: dict) -> Path:
    d = Path(cfg["work_dir"] or (SKILL_ROOT / "work"))
    d.mkdir(parents=True, exist_ok=True)
    return d
