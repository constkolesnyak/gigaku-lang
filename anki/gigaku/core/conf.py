"""Config: per-feature sections, plus the reads of *other* add-ons' settings.

AnkiMorphs' status tags live in ONE place now — `am_tags()`, read from the profile's own
`ankimorphs_profile_settings.json` — where they used to exist three times (jk read the
profile, smc hardcoded copies in its config, lib/config.py hardcodes the CLI's). Rename a
tag in AnkiMorphs and this follows; the literals below are only the fallback for a profile
that has never saved settings.
"""
import json
import os

from aqt import mw


MVJ_ADDON_DIR = "MvJ Japanese"

DEFAULTS = {
    "debug": False,
    "counter": {
        "enabled": True,
        "notetype": "🇯🇵 MvJ",
        # Every note type the counter passes over. `notetype` (singular) stays: it is what
        # nav/browser_view/keys scope the ja-only views to, and the fallback when a user
        # config predates the list. Removing 🇩🇪 German here is the German off-switch.
        "notetypes": ["🇯🇵 MvJ", "🇩🇪 German"],
        "counter_field": "My Alternatives",
        "sort_field": "My Sort Key",
        "study_field": "am-study-morphs",
        "allcount_field": "am-all-morphs-count",
        "clarity_field": "My Clarity",
        # Read only for the my-learn tiebreak — never written by this add-on.
        "sentence_field": "Sentence",
        "learn_tag": "my-learn",
        # The study decks are NOT here: they are MvJ's MediaConfig (Settings ▸ General,
        # one per language) and are read via `study_deck_for` — one source, one dialog.
    },
    "nav": {
        "enabled": True,
        "goto_hotkey": "J",
        # K opens the German queue exactly as J opens the Japanese one. my-learn and the
        # status tags are collection-wide, so each key's query is scoped to its note type
        # — J is Japanese-only by the user's rule (2026-08-06, "do not mix languages").
        # Main-window keys: the Browser's own j/k row moves (keys section) are untouched.
        "goto_de_hotkey": "K",
        "de_notetype": "🇩🇪 German",
        # A search for K to open instead of the German queue — "" keeps the queue.
        # User's ask (2026-08-26): K opens deck:"Frequency 🇩🇪", the generated-sentence
        # deck reviewed in place; the my-learn queue is still one L away.
        "goto_de_query": "",
        "alternates_hotkey": "L",
        "sort_descending": True,
    },
    "keys": {
        "enabled": True,
        # Overrides am_tags()["known_manually"] when set — same as the old jk `known_tag`.
        "known_tag": "",
        # The browse keys, and the only place they are set. Any Qt sequence ("M",
        # "Ctrl+Alt+K"); empty switches one off. `known_toggle_hotkey` is `M` because `I`
        # is MvJ's View Context — one key, one binding, and the two must not both claim it.
        "row_down_hotkey": "J",
        "row_up_hotkey": "K",
        "known_toggle_hotkey": "M",
        "flag_translate_hotkey": "N",
        "trim_audio_hotkey": "U",
        "ask_question_hotkey": "Y",
    },
    "am_toolbar": {"enabled": True},
    # Which languages an AnkiMorphs recalc touches (features/am_recalc.py). Untick a
    # language to pause it: R stops rewriting its cards and its queue freezes as the last
    # full recalc left it. Applied per recalc — OK in the dialog is enough, no restart.
    "recalc": {"enabled": True, "ja": True, "de": True},
    # Puts the Frequency deck's own queue order back after each recalc (see
    # features/freq_order.py); the order itself is stored in the collection by
    # freqdeck/order.py, so this only replays a list.
    "freq_order": {"enabled": True},
    "merge": {"enabled": True},
    "mvj_retunes": {"enabled": True},
    "hotkey_ui": {"enabled": True},
    "trimmer": {"enabled": True},
    "cyrillic": {"enabled": True},
    "browser_view": {"enabled": True},
}


def _raw():
    try:
        return mw.addonManager.getConfig(__name__) or {}
    except Exception:  # noqa: BLE001
        return {}


def debug():
    return bool(_raw().get("debug", DEFAULTS["debug"]))


def section(name):
    """One feature's config: its defaults, with the user's keys layered on top."""
    merged = dict(DEFAULTS.get(name) or {})
    merged.update(_raw().get(name) or {})
    return merged


def enabled(name):
    return bool(section(name).get("enabled", True))


# ── other add-ons' settings ──────────────────────────────────────────────────


# ── the remembered browser view ──────────────────────────────────────────────

VIEW_STATE_KEY = "gigaku.viewState"


def view_state():
    """Columns (ordered), widths, sort column and direction — ONE state for all three
    gigaku views (the my-learn queue, L's alternates, O's episode context).

    The user shapes it by hand in the browser — drag a column, resize it, click a sort —
    and the add-on remembers and re-applies it on every arrival, identical everywhere.
    Stored in the collection config so it survives restarts and syncs with the
    collection; the default below is only the state of a browser nobody has adjusted yet.
    """
    from aqt import mw

    counter = section("counter")
    default = {
        "columns": [
            f"_field_{counter['study_field']}",
            f"_field_{mvj_field('word', 'Word')}",
            f"_field_{counter['clarity_field']}",
            f"_field_{counter['sort_field']}",
        ],
        "widths": {f"_field_{counter['sort_field']}": 44},
        "sort": f"_field_{counter['sort_field']}",
        "descending": bool(section("nav").get("sort_descending", True)),
    }
    try:
        stored = mw.col.get_config(VIEW_STATE_KEY, None) or {}
    except Exception:  # noqa: BLE001
        stored = {}
    merged = dict(default)
    for key in ("columns", "widths", "sort", "descending"):
        if key in stored:
            merged[key] = stored[key]
    return merged


# The episode context (MvJ's O) is the one gigaku view with an order of its own: those
# cards *are* an episode, so they read in the order they were watched. That is note
# creation order — MvJ builds an episode's notes in order as it makes them — and it is
# MvJ's own answer too: its View Context ends with
# `show_and_sort_by_column(browser, "noteCrt", descending=False)`. Ascending, because
# descending plays the episode backwards. (The audio filename carries the real timestamp,
# but in two formats and with no zero padding — `0.9.…` would sort after `0.10.…` — so
# text-sorting that field is a worse answer, not a better one.)
CONTEXT_SORT = ("noteCrt", False)


def view_sort(kind):
    """(column key, descending) for a gigaku view — the one place that answers it.

    The remembered order everywhere except the episode context. That view's order is put on
    the *search* and never written to the sort state (`browser_view`), so "the other views
    keep the order I gave them" holds by construction rather than by being restored
    afterwards."""
    if kind == "context":
        return CONTEXT_SORT
    state = view_state()
    return state["sort"], bool(state["descending"])


def save_view_state(state):
    from aqt import mw

    try:
        mw.col.set_config(VIEW_STATE_KEY, {
            "columns": list(state["columns"]),
            "widths": dict(state["widths"]),
            "sort": state["sort"],
            "descending": bool(state["descending"]),
        })
    except Exception:  # noqa: BLE001
        pass


def mvj_config():
    try:
        return mw.addonManager.getConfig(MVJ_ADDON_DIR) or {}
    except Exception:  # noqa: BLE001
        return {}


def mvj_field(key, default):
    """A field name out of MvJ's own settings — they are configurable, so never assumed."""
    return (mvj_config().get("fields") or {}).get(key) or default


# Reading MvJ's Hotkeys rows for this add-on's own keys, and writing its defaults into
# them, were both tried across one evening and both taken back out. Those rows are not
# labels: a filled row is what makes *MvJ* bind its own version of the action. Every row
# naming a key this side already held put two claims on that key, and Qt answers two
# claims by firing neither — which is what `j` dying, View Context doing nothing and
# "the shortcuts are slow and glitchy" all were. The keys live in this add-on's config;
# `mvj_hotkey` stays for the settings that are genuinely MvJ's to answer (field names,
# its browser query).


def mvj_hotkey(name):
    """A key out of MvJ's own **Settings ▸ Hotkeys** tab, or "" when that row is empty.

    That tab is a real editor with a key-capture widget in it, and it already lists the
    actions this add-on reimplements — Tag as Known, Browse Alternates, Go-to Browser
    Search, View Context. So it is where those keys are set, and this add-on reads them
    rather than keeping a second, invisible copy: a row filled in there wins, an empty row
    falls back to the default in gigaku's own config. A shortcut nobody can find is a
    shortcut nobody can change.

    Only rows whose action is *the same action* are read. MvJ's "Add Question" row is bound
    to its own bulk-capable version, not to the one-note-at-a-time wrapper on `y`, so
    binding ours there would quietly take a key the user had set for something else."""
    return ((mvj_config().get("hotkeys") or {}).get(name) or "").strip()


def mvj_browser_query():
    return (mvj_config().get("browser_search_query") or "").strip()


def study_deck_for(notetype):
    """The study deck for one note type — read from MvJ's MediaConfig, the single source.

    The decks live in MvJ Settings ▸ General (Study Deck 🇯🇵 / Study Deck 🇩🇪) since the
    dialog became the one home for the whole setup (2026-08-06); keeping a second copy in
    this add-on's config was two sources for one fact. Reading MvJ's settings is fine —
    it is *writing* them that the hotkeys story forbids.
    """
    media = mvj_config().get("media") or {}
    if notetype == ((media.get("german_notetype") or "🇩🇪 German").strip()):
        return (media.get("study_deck_de") or "Study 🇩🇪").strip()
    return (media.get("study_deck") or "Study 🇯🇵").strip()


def am_tags():
    """AnkiMorphs' status tags, read live from the profile settings so they match the
    user's values; `known_manually` is overridable via the keys section's `known_tag`."""
    tags = {
        "known_manually": "_card-status::i+0-manually",
        "known_auto": "_card-status::i+0",
        "ready": "_card-status::i+1",
        "not_ready": "_card-status::i+≥2",
    }
    try:
        path = os.path.join(mw.pm.profileFolder(), "ankimorphs_profile_settings.json")
        with open(path, encoding="utf8") as handle:
            settings = json.load(handle)
        for key, setting in (("known_manually", "tag_known_manually"),
                             ("known_auto", "tag_known_automatically"),
                             ("ready", "tag_ready"), ("not_ready", "tag_not_ready")):
            if settings.get(setting):
                tags[key] = settings[setting]
    except Exception:  # noqa: BLE001
        pass
    override = section("keys").get("known_tag")
    if override:
        tags["known_manually"] = override
    return tags
