"""Constants and settings for gigaku.

Two kinds of knobs live here:

* Plain module constants — the hardware/browser facts the Apple TV daemon and the Netflix
  subtitle exporter need. They never change per run.
* ``settings`` — the word-tracking side (import / plots / kanji / anki / srt), ported from
  polyglotka. Those *are* worth tweaking per run, so each field reads from a ``GIGAKU_``
  environment variable and can be overridden again by a CLI flag (see lib/cli.py).
  polyglotka did this with pydantic-settings; a dataclass plus ``os.environ`` is the same
  thing in 40 lines and keeps gigaku's dependency list at three.
"""
import contextlib
import os
import sys
import threading
from dataclasses import dataclass, fields

# Per-thread tag for `note`. `gigaku subs` glosses one episode while the browser rips the
# next, so two jobs narrate at once and "request 3/7" alone can't say which episode it
# belongs to. Thread-local rather than a parameter because the lines come from deep inside
# lib/subs/translate.py, which knows nothing about running beside anything.
_TAG = threading.local()


def note(*args):
    """Progress and status go to stderr — stdout belongs to the command's actual output.

    `gigaku kanji > kanji.tsv` and `gigaku anki | pbcopy` both send stdout somewhere that
    would choke on "Cached 27,513 words."; keeping chatter on stderr is what lets them."""
    tag = getattr(_TAG, "tag", "")
    if tag:
        text = " ".join(str(a) for a in args)
        # The message keeps its own shape; the tag replaces the indent it was written with,
        # so a tagged line still lines up under the untagged ones around it.
        print(f"{tag} {text.strip()}" if text.strip() else text, file=sys.stderr)
        return
    print(*args, file=sys.stderr)


@contextlib.contextmanager
def noting(tag: str):
    """Tag every ``note`` from this thread with ``tag`` for the duration."""
    previous = getattr(_TAG, "tag", "")
    _TAG.tag = tag
    try:
        yield
    finally:
        _TAG.tag = previous

# ── Apple TV (mouse-wheel volume; lib/appletv/control.py, scripts/scroll_volume.py) ───────────

# Companion credentials live in this pyatv FileStorage, written by scripts/pair_appletv.py.
# Gitignored.
APPLETV_STORAGE_PATH = os.path.join(os.path.dirname(__file__), "..", ".atv_storage")

# mDNS hostname of the Apple TV. lib.appletv.connect() resolves it to the current IP each
# run and connects there by unicast host-scan — pyatv's own multicast device scan is
# unreliable on this network (the AP's per-SSID client isolation drops/garbles it; see the
# lan_bypass daemon). The name is derived from the device's hardware id, so it's stable and
# resolving it stays DHCP-proof. Find it:
#   dns-sd -L "<name>" _companion-link._tcp   (prints "<host>.local.:<port>")
APPLETV_HOSTNAME = "Living-Room-02ADBE0DEE01.local"

# The Apple TV's hardware MAC — the hex baked into APPLETV_HOSTNAME (…02ADBE0DEE01).
# Unlike the mDNS name (which wedges under the AP's client isolation) and the DHCP IP, a MAC
# never changes. So it's the robust, mDNS-free way to find the device: grep the
# ARP/neighbour table for it. Both lan_bypass.sh and _resolve_ip() fall back to this.
# macOS's `arp` prints MACs with per-octet leading zeros stripped (04 -> 4), so compare
# normalised, not literally.
APPLETV_MAC = "02:ad:be:0d:ee:01"

# Python's own mDNS resolution of APPLETV_HOSTNAME is intermittent. The lan_bypass daemon
# publishes the IP it resolves here; connect() reads it as a fallback. Kept in /tmp so the
# root daemon and the user agent both reach it. If it's missing too, _resolve_ip() finds the
# IP by APPLETV_MAC in the ARP table — no mDNS, no daemon, no cache required.
APPLETV_IP_CACHE = "/tmp/gigaku-atv-ip"

# ── Samsung TV (the wheel's fallback; lib/samsung/control.py) ─────────────────────────

# When the Apple TV can't be reached — off, asleep, off the network — the wheel drives the
# TV's own volume instead of doing nothing. Same physical result: the Apple TV only relays
# volume to this TV over HDMI-CEC anyway.

# The TV's hardware MAC, read off its own description (`GET http://<ip>:8001/api/v2/`,
# field `wifiMac`) and confirmed against the ARP table. A MAC survives what neither an IP
# nor mDNS does, so it is how the TV is found. Model GQ55Q80DATXZG, wired to the router —
# which is why it stays reachable while the Apple TV needs the lan_bypass ARP pin (that
# isolation is per-SSID and the TV isn't on the Wi-Fi at all).
SAMSUNG_MAC = "02:5a:00:01:02:03"

# Last IP the TV was found at, written by lib/samsung whenever it resolves one. A cache,
# never the truth: the previous hardcoded constant (192.168.0.198) was already stale — the
# TV had moved to .42 — which is the whole argument for MAC-then-scan over a fixed IP.
SAMSUNG_IP_CACHE = "/tmp/gigaku-tv-ip"

# The token the TV hands back after you accept its one-time "Allow this device?" dialog.
# Replayed on every later connection to skip the popup; gitignored, and the same file the
# pre-Apple-TV volume daemon paired into, so nothing needs re-accepting.
SAMSUNG_TOKEN_PATH = os.path.join(os.path.dirname(__file__), "..", ".tv_remote_token")

# ── Netflix + Language Reactor subtitle export (`gigaku subs`; lib/subs/subs.py) ───

# Any Netflix watch tab is matched by this URL substring (exec_js_on_extension). The wider
# NETFLIX_MATCH exists to tell the two apart: "no tab is on a /watch page" is NOT the same as
# "the tab was closed" — Netflix bounces its own tab to /browse, /title/… or a login page, and
# that tab is still open and still steerable. Reporting the first as the second ended runs
# with a message that was simply untrue.
NETFLIX_WATCH_MATCH = "netflix.com/watch"
NETFLIX_MATCH = "netflix.com"

# Language Reactor exports .xlsx via a browser download (default macOS location); the
# exporter watches this directory for the new file, then converts + moves the result.
CHROME_DOWNLOAD_DIR = os.path.expanduser("~/Downloads")

# The subtitle library — where the converted SRT pairs live, and the default SRT_TARGET_DIR.
# It sits *inside* the chrome/gigaku extension on purpose: a Chrome extension can fetch
# its own files with no server, no file:// permission and no folder picker, which is what lets
# the extension hand the right pair to Migaku the moment an episode starts. Gitignored.
SUBS_LIBRARY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chrome", "gigaku", "subs"
)

# Waits are adaptive, not fixed timeouts: each polls for forward progress (playback
# advancing, subtitle/translation counts rising, a new file appearing) and blocks as long as
# progress continues — riding out a slow Language Reactor, returning immediately on a fast
# one. It gives up only after this many seconds with NO progress at all (a genuine stall).
NF_STALL_GIVEUP = 120

# ── Word tracking (`import`, `plots`, `kanji`, `anki`, `words`, `srt`) ────────

# gigaku's own word cache. It is the *only* durable record of your vocabulary history:
# Language Reactor / Migaku exports are deleted once imported (RM_PROCESSED_FILES), and
# neither service keeps a per-day history you can re-download. Back it up, don't delete it.
CACHE_DIR = os.path.expanduser("~/Library/Caches/gigaku")
WORDS_CACHE = os.path.join(CACHE_DIR, "words.json")

# polyglotka's cache, in the same JSON schema. lib/vocab/words.py adopts it once, automatically,
# if gigaku has no cache yet — so uninstalling polyglotka doesn't cost you your history.
LEGACY_WORDS_CACHE = os.path.expanduser("~/Library/Caches/polyglotka/words.json")

# Written once the adoption above has run. Without it, `gigaku clear-cache` would be undone
# by the next read (no gigaku cache → adopt polyglotka's again), which is not what "clear"
# means. It records that the migration is done, whether or not a cache exists now.
CACHE_ADOPTED = os.path.join(CACHE_DIR, ".polyglotka-adopted")

# ── `gigaku clarity` — rate i+1 alternates by context (lib/anki/) ─────────────────────

# Facts about the collection, not per-run knobs. They mirror the gigaku add-on's
# config.json: the add-on owns the *sort key*, this side only fills My Clarity. These
# are the JAPANESE profile's constants; `clarity_profile(lang)` below binds a run to a
# language — German landed 2026-08-07 exactly as promised: a second named rubric
# (prompt_de), the same field names, its own cache beside CLARITY_CACHE. (An earlier
# version of this comment gated that promise on a `clarity.require_lang` that never
# existed anywhere in the repo.)
CLARITY_NOTETYPE = "🇯🇵 MvJ"
CLARITY_READY_TAG = "_card-status::i+1"  # AnkiMorphs' tag_ready — one unknown morph
CLARITY_SENTENCE_FIELD = "Sentence"      # AnkiMorphs' configured expression field
CLARITY_MORPH_FIELD = "am-study-morphs"  # the one unknown morph on an i+1 card
CLARITY_ALLCOUNT_FIELD = "am-all-morphs-count"  # morphs in the sentence; the cheap proxy
CLARITY_ALTERNATES_FIELD = "My Alternatives"    # group size, kept current by the add-on
# The add-on owns this format and rewrites it on every recalc; `gigaku clarity` writes the
# same value as it scores so a card doesn't sit with a stale key until you remember to
# press R. See lib/anki/store.sort_key.
CLARITY_SORT_FIELD = "My Sort Key"
# The one card per word you actually study. The add-on recomputes it after every recalc;
# `gigaku clarity` applies the same rule as it scores so the run is visible right away.
CLARITY_LEARN_TAG = "my-learn"
CLARITY_STUDY_DECK = "Study 🇯🇵"  # words with a card here are already being learnt — skipped
CLARITY_FIELD = "My Clarity"             # what we write: "0.00".."1.00"
# Which run produced the score. A plain integer, bumped once per `gigaku clarity`
# invocation, so any card can be traced back to the pass that judged it — the rubric and the
# model have both changed under this collection more than once, and without a stamp there is
# no way to tell a card scored under one from a card scored under another. What each number
# means (model, when, how many cards) is kept beside the scores in CLARITY_CACHE.
#
# Its own field rather than a corner of `Notes`: 82 notes carry real translations there, and
# a field that is ours alone can be overwritten freely.
CLARITY_RUN_FIELD = "My Run"
CLARITY_RUN_FIELD_DESCRIPTION = (
    "Which `gigaku clarity` pass wrote this card's My Clarity — a plain run number. The "
    "rubric and the model change over time, so this is what tells scores from different "
    "passes apart. Run details live in ~/Library/Caches/gigaku/clarity.json."
)
CLARITY_FIELD_DESCRIPTION = (
    "0..1 — how obvious this card's am-study-morphs is from the Sentence alone, judged by "
    "Claude for a learner who knows every other word in the sentence. 1 = fully inferable "
    "from context. Written by `gigaku clarity`; feeds My Sort Key, so alternates (L in the "
    "Browser) surface the most guessable example first. Empty = not scored yet."
)

# Anki's own HTTP API (add-on 2055492159), bound to loopback with no key. `gigaku clarity`
# reads and writes the collection only through this — never by opening collection.anki2,
# which Anki owns while it is running.
ANKICONNECT_URL = "http://127.0.0.1:8765"

# Scores keyed by the (sentence, morph) pair rather than the note id, so a re-import, a
# cleared field or a card leaving and re-entering i+1 never pays for the same judgement
# twice. Scoring is slow (~5 min per 200 cards through the CLI), so this cache is what
# makes an interrupted run cheap to resume. Deliberately NOT in backup.MANAGED: unlike
# words.json it is re-derivable, and it also lives in the synced collection.
CLARITY_CACHE = os.path.join(CACHE_DIR, "clarity.json")
# German scores live in their own file: store.last_rubric() reads a cache's newest run
# regardless of language, so one shared file would false-restale ja on every alternation.
CLARITY_CACHE_DE = os.path.join(CACHE_DIR, "clarity_de.json")


def clarity_profile(lang):
    """One language's clarity facts — notetype, deck, cache, rubric — as a namespace.

    The field names are identical across the note types by design (apkg_export_de
    mirrors 🇯🇵 MvJ), so only the notetype, the study deck and the cache file differ.
    The rubric rides along so every consumer gets the matching wording and fingerprint
    from the same object it gets the cache path from — the pair must never split.
    """
    from types import SimpleNamespace

    from lib.anki import prompt

    if lang not in ("ja", "de"):
        raise UserError(f"--lang must be ja or de, not {lang!r}")
    system, fingerprint = prompt.rubric(lang)
    return SimpleNamespace(
        lang=lang,
        notetype="🇩🇪 German" if lang == "de" else CLARITY_NOTETYPE,
        ready_tag=CLARITY_READY_TAG,
        sentence_field=CLARITY_SENTENCE_FIELD,
        morph_field=CLARITY_MORPH_FIELD,
        allcount_field=CLARITY_ALLCOUNT_FIELD,
        alternates_field=CLARITY_ALTERNATES_FIELD,
        sort_field=CLARITY_SORT_FIELD,
        learn_tag=CLARITY_LEARN_TAG,
        study_deck="Study 🇩🇪" if lang == "de" else CLARITY_STUDY_DECK,
        field=CLARITY_FIELD,
        run_field=CLARITY_RUN_FIELD,
        cache=CLARITY_CACHE_DE if lang == "de" else CLARITY_CACHE,
        rubric=system,
        fingerprint=fingerprint,
    )

# Migaku dictForm → spaCy lemma, for the German known-morphs CSV (lib/vocab/words.py).
# Re-deriving a lemma costs a subprocess into the AnkiMorphs spaCy venv, so hits are
# cached; like the other caches this re-derives, so it is not in backup.MANAGED.
DE_LEMMA_CACHE = os.path.join(CACHE_DIR, "de_lemmas.json")


class UserError(Exception):
    """A mistake the user can fix (bad flag, missing export, no cache). Printed without a
    traceback by lib/cli.py — unlike a bug, there is nothing to debug."""


def _env(name, default):
    """Read GIGAKU_<NAME>, coerced to type(default). Booleans accept true/false/1/0/yes/no."""
    raw = os.environ.get(f"GIGAKU_{name}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise UserError(f"GIGAKU_{name} must be an integer, not {raw!r}")
    return raw


@dataclass
class Settings:
    # Where LR/Migaku exports land, and whether they're deleted once imported.
    EXPORTED_FILES_DIR: str = os.path.expanduser("~/Downloads")
    RM_PROCESSED_FILES: bool = True
    LR_WORDS_GLOB: str = "lln_json_items_*.json"
    LR_SUBS_GLOB: str = "lln_excel_subs_*.xlsx"
    MIGAKU_WORDS_GLOB: str = "migaku_words_*.csv"

    # `gigaku srt` and `gigaku subs` write the converted SRT pairs into SRT_TARGET_DIR/<Title>/
    # — the subtitle library, not the download folder they came from. It defaults to the
    # chrome/gigaku extension's own subs/ folder, so a fresh rip is instantly loadable by
    # the extension (see SUBS_LIBRARY_DIR). Empty means "next to the exports"
    # (EXPORTED_FILES_DIR).
    SRT_TARGET_DIR: str = SUBS_LIBRARY_DIR
    # Before writing a season, existing SRT pairs in that directory are moved here — so a
    # re-run of a shorter season can't leave last week's E09–E12 lying beside the new E01–E08,
    # silently mixing two rips. Empty means "don't move anything, just overwrite".
    SRT_TRASH_DIR: str = ""
    # LR's export gives a start time per line but no end time, so the end is estimated from
    # how long the line takes to read.
    LR_SUBS_MS_PER_CHAR: int = 80

    # The Secondary (Russian) track is glossed from the ripped German one by Claude
    # (lib/subs/translate.py) rather than machine-translated by Language Reactor, which is
    # what the rip used to wait on — line by line, rate-limited by the season, and blind to
    # everything but the line in front of it.
    #
    # Off (`--no-translate`) restores the old rip exactly: LR's Machine Translation column is
    # ticked again, `_wait_translations` runs, and the Secondary comes out of the same xlsx.
    # Worth keeping, because that path needs no `claude` on PATH and no subscription.
    SUBS_TRANSLATE: bool = True
    SUBS_TRANSLATE_MODEL: str = "opus"
    # Not a knob so much as a fix: without it a request answers in one of two modes and the
    # cheap one silently drops two thirds of the German compounds (see CLAUDE.md, 2026-08-07).
    SUBS_TRANSLATE_EFFORT: str = "high"
    # One extra call per episode over its ~30 distinct joined tokens, putting each part into
    # dictionary form. Seven rubric edits got that from ~17 bent tokens per episode to ~6 and
    # then plateaued; this pass takes the rest. Off restores the glosser's raw output.
    SUBS_TRANSLATE_NORMALISE: bool = True
    # Lines per request. Still far bigger than an API call would want, for the reason
    # lib/claude.py records: every `claude -p` drags Claude Code's ~24k-token harness with it,
    # so the invocation is what needs amortising. **200 was that argument taken too far, and
    # 50 is the user's decision of 2026-08-08 — "do it properly".** Measured on E06, same
    # rubric, same 144 cues: a 200-cue request found 4 German compounds where 50-cue requests
    # found 14, collapsing the rest into single Russian words. Amortising the harness is worth
    # doing right up until it starts costing the thing the harness is being paid for. The
    # price of the change is real and was accepted knowingly: 7 invocations instead of 2,
    # ~$4 instead of ~$1.25, ~4 minutes instead of 2.
    SUBS_TRANSLATE_GROUP: int = 50
    # Untranslated neighbours shown on either side of a request, so the seam between two
    # requests isn't a cliff. Not optional padding: LR's exporter cuts sentences mid-clause,
    # and a fragment with no neighbour visible cannot be translated at all.
    SUBS_TRANSLATE_CONTEXT: int = 6
    # Re-asks for the lines a request didn't answer, before the episode is called unfinished.
    SUBS_TRANSLATE_RETRIES: int = 2

    # The ripped German is ASR — a machine transcript with machine mistakes in it. Every line
    # is read by Claude before the glosser sees it (lib/subs/spell.py), so the Russian is
    # glossed from corrected German rather than from what the transcriber guessed. Measured
    # over the 165 ripped episodes: the export itself is byte-faithful (no mojibake, no
    # stripped umlauts, no run-together words), and what is wrong is the transcript — a name
    # spelled differently every time it is said (~28 minority occurrences an episode, the
    # commonest and most visible defect), ~3–5 misspellings, ~2.4 nouns left lowercase.
    #
    # It hangs off the gloss rather than off the rip, and that is structural rather than a
    # second switch: on --no-translate the Secondary is Language Reactor's translation of the
    # *exported* German, written in the same call as the Primary, so the path that "needs no
    # claude on PATH" must not grow a claude call. `gigaku spell` proofreads on demand.
    SUBS_SPELL: bool = True
    SUBS_SPELL_MODEL: str = "opus"
    # Opus at high effort for the same measured reason the glosser uses them: left to the
    # default a request answers in one of two modes and the cheap one is quietly worse.
    SUBS_SPELL_EFFORT: str = "high"
    # Lines per request. Bigger than the glosser's 50 on purpose — 50 was measured to protect
    # the *compound splitting* the gloss does, a judgement this pass does not make, while the
    # reply here is almost all `OK` and costs little to produce. The invocation is still what
    # needs amortising (lib/claude.py's ~24k harness), so an episode is ~4 requests, not ~8.
    SUBS_SPELL_GROUP: int = 100
    # Unasked neighbours on either side of a request. LR's exporter cuts sentences mid-clause,
    # so a cue read alone routinely looks broken when it is fine — the seam is the one place
    # this pass would invent a correction, and context is what stops it.
    SUBS_SPELL_CONTEXT: int = 4
    SUBS_SPELL_RETRIES: int = 1

    # Migaku has no export button worth clicking: its word list sits in Chrome's IndexedDB,
    # so `gigaku import` reads it straight off disk (lib/vocab/migaku.py). Turn off with --no-chrome.
    CHROME: bool = True
    CHROME_DATA_DIR: str = ""  # empty → the standard macOS Chrome profile location
    # The binary, for the one job that needs Chrome *headless* rather than the user's own
    # running window: `gigaku report` screenshots its cards with it (lib/vocab/report.py).
    CHROME_APP: str = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    # AnkiMorphs known-morphs CSVs, written on every import for these languages. The
    # defaults are the live reality (2026-08-07): the profile's own known-morphs folder
    # and both studied languages — because the DAILY writer is `gigaku backup` under
    # launchd, which runs via `/bin/sh -c` and never sees the login shell's GIGAKU_*
    # exports. A default of "" here meant the nightly run silently saved nothing and
    # AnkiMorphs never learnt what Migaku taught that day.
    KNOWN_MORPHS_DIR: str = os.path.expanduser(
        "~/Library/Application Support/Anki2/MainProfile/known-morphs"
    )
    KNOWN_MORPHS_SAVE_LANGS: str = "ja,de"

    # `gigaku anki` builds a search query for the kanji you know the most words with.
    ANKI_MIN_COUNTS: str = "0,0"  # min (known, learning) word counts for a kanji to make it
    ANKI_FILTERS: str = "deck:漢字 is:suspended"
    ANKI_KANJI_FIELD: str = "kanji"

    # `gigaku plots` renders a self-contained HTML page (lib/vocab/plots.py) and opens it.
    PLOTS_TITLE: str = "Vocabulary"
    PLOTS_OUT: str = os.path.join(CACHE_DIR, "plots.html")
    # Languages that load with their line toggled off — they stay in the legend, one click
    # away. Defaults to the two that are done rather than in progress: English (a 10k
    # native-language dump that flattens everything else) and French.
    PLOTS_HIDDEN_LANGS: str = "en,fr"

    # `gigaku publish` rsyncs the rendered page into PUBLISH_DIR; a small static server
    # serves it at PUBLISH_URL, tailnet-only — the port is bound to the Mac's
    # Tailscale IP, and tailnet-only matters: the kanji table lists the actual words you
    # know. No server can gather any of this itself (the word cache and Chrome's Migaku
    # database are on this Mac, and a web page can't read another origin's IndexedDB), so
    # the Mac renders and pushes. PUBLISH_HOST is empty = local copy; set it to an ssh
    # host and the old push-to-a-server shape (the retired Pi) comes back unchanged.
    PUBLISH_HOST: str = ""
    PUBLISH_DIR: str = os.path.expanduser("~/Sites/gigaku")
    PUBLISH_URL: str = "http://100.100.100.100:8900/gigaku/"

    # `gigaku report` sends the week's progress to Telegram: one chart card per language,
    # drawn by lib/vocab/report_card.html and screenshotted with headless Chrome.
    # REPORT_LANGS empty means "every language the plots page doesn't hide", i.e. the ones
    # actually in progress — the same judgement, made once, in PLOTS_HIDDEN_LANGS.
    REPORT_LANGS: str = ""
    REPORT_WEEKS: int = 12  # bars on the card: a season of context behind one week's number
    REPORT_DIR: str = os.path.join(CACHE_DIR, "report")
    # Every report run also writes the week as a page here (<date>.html per week +
    # index.html); `gigaku publish` copies the whole directory next to the chart page, so
    # the site carries the report history. Re-derivable from vocab-backup if this dies.
    REPORT_SITE_DIR: str = os.path.join(CACHE_DIR, "site", "report")
    # The bot token and chat id live *here*, not in Settings: `gigaku info` prints every
    # setting, and a token that can be printed is a token that ends up in a log.
    TELEGRAM_ENV_FILE: str = os.path.expanduser("~/.config/gigaku/telegram.env")

    # `gigaku backup` snapshots the three irreplaceable datasets — the word cache, Migaku's
    # full raw word list (every status, including the UNKNOWN/IGNORED the store drops), and
    # AnkiMorphs' hand-curated CSVs — as plain text into BACKUP_DIR, a git repo, and pushes to
    # BACKUP_REMOTE. The point of a *versioned* backup: any past day restores with one
    # `git checkout`. Empty BACKUP_REMOTE commits locally without pushing.
    BACKUP_DIR: str = os.path.expanduser("~/vocab-backup")
    BACKUP_REMOTE: str = "origin"
    # Anki profile dir holding ankimorphs.db + the known-morphs/ and priority-files/ CSVs.
    ANKI_PROFILE_DIR: str = os.path.expanduser(
        "~/Library/Application Support/Anki2/MainProfile"
    )
    # The languages AnkiMorphs contributes known words for (comma list), or "" to switch
    # that source off. AnkiMorphs' morphs carry no language of their own — the tables have
    # no language column — so with two note filters in one collection a lemma's *script* is
    # the only witness (kana/Han/fullwidth → ja, Latin → de; see lib/vocab/ankimorphs.py).
    # Measured 2026-08-06 before German arrived: 3,793 of 3,800 known lemmas were CJK, the
    # other 7 fullwidth netspeak (ｗ, ｋｗｓｋ) the fullwidth block keeps Japanese, 0 Latin —
    # so "ja,de" reproduces the old single-language behaviour exactly until German cards
    # exist. Best-effort: no Anki running just means Migaku/LR as before.
    ANKIMORPHS_LANGS: str = "ja,de"
    # Refuse to commit a snapshot where any dataset shrank by more than this percent — a
    # logged-out Chrome or a wiped language must not overwrite a good backup with a hollow one.
    # `gigaku backup --force` overrides it for a legitimate mass-delete.
    BACKUP_SHRINK_PCT: int = 10
    # A backup whose Migaku data hasn't moved in this many days is committed but reported
    # as *stale* (Telegram alert): a frozen-but-readable source is invisible to the shrink
    # gate — measured 2026-07, one origin froze for nine silent days — and only the data's
    # own last-change date can tell "frozen" from "a quiet week". 5 days keeps a long
    # weekend from crying wolf.
    BACKUP_STALE_DAYS: int = 5

    # `gigaku titles` rips a Netflix browse gallery and joins it with IMDb ratings, to answer
    # "what here is actually worth watching". The URL is a default, not a hardcode — the
    # positional argument wins.
    TITLES_URL: str = "https://www.netflix.com/browse/audio/81510840/de"
    TITLES_DIR: str = os.path.join(CACHE_DIR, "netflix")  # the rip's dumps, one per gallery
    TITLES_OUT: str = os.path.join(CACHE_DIR, "titles.html")
    TITLES_CSV: str = os.path.join(CACHE_DIR, "titles.csv")
    # Reuse a *complete* dump younger than this instead of re-ripping. A gallery of 4,000
    # titles changes slowly, and a re-rip is minutes of API calls. 0 = always re-rip.
    TITLES_MAX_AGE_DAYS: int = 7
    # Falcor window width. 50 is measured-good; the endpoint may reject a much wider one, and
    # a narrower one just means more round trips.
    TITLES_PAGE_SIZE: int = 50
    # How much of the JSON to carry back per AppleScript call. The reply crosses an Apple event
    # as a *string*, so a whole 50-title window (~60 KB) is read in slices and length-checked —
    # a silently truncated string would parse as a short-but-valid gallery.
    TITLES_CHUNK_BYTES: int = 32768
    # Pause between windows. The endpoint rate-limits bursts: parallel windows fail with
    # "xhr error" and then succeed moments later, so the rip stays deliberately unhurried.
    TITLES_REQUEST_DELAY_MS: int = 250
    TITLES_MAX: int = 20000  # backstop so a paging bug can't loop forever
    # Which of the genre node's lists to walk. `su` ("Suggestions For You") is the only one
    # the browse page itself requests; `az`/`za`/`yr` are the sort orders Netflix removed
    # from the web UI and kept in pathEvaluator (measured 2026-07-28 — `sr`/`ry` are refused,
    # so this is a closed set like FIELDS). Walking them all and merging by id is what stops
    # one personalised list from deciding what the catalogue contains. They agreed to within
    # eight titles on the German gallery, which is the useful measurement: the storefront is
    # not personalised down, so a title missing from all four is missing from the *node*.
    TITLES_SUBLISTS: str = "su,az,za,yr"
    # Titles to pull in by Netflix id or /title/ URL, comma-separated. The dub gallery is not
    # the catalogue — My Hero Academia (80135674) plays with a German audio track and is in
    # none of the node's lists — so a title you know is dubbed can be named here and it joins
    # the page tagged NEW. `videos/<id>` resolves the same FIELDS a gallery slot does, so
    # these are the same records, not a second format.
    TITLES_EXTRA_IDS: str = ""
    # Genres to leave out of the page. A title is dropped if *any* of its IMDb genres is
    # listed, so "Crime" also takes the crime-dramas. This filters the page only — the CSV and
    # the JSON dump still hold every title, so nothing is lost by changing your mind. Empty
    # shows everything.
    TITLES_HIDE_GENRES: str = "Crime,Documentary"
    # Markers to attach, from Netflix's own genre galleries (lib/netflix/markers.py): its
    # classification, not a guess from IMDb genre strings — "Animation" would call Rick and
    # Morty anime. Each one costs a gallery rip the first time, then caches. Empty = none.
    TITLES_MARK: str = "anime,korean"
    # Titles per `videos/[ids]/genres` request. Falcor batches ids happily; the ceiling is the
    # AppleScript round-trip, and the reply is small (a handful of genre names per title).
    TITLES_GENRE_BATCH: int = 40

    # IMDb's official datasets (https://datasets.imdbws.com/, refreshed daily, no key, no
    # rate limit) are the ratings source. They are downloaded once, reduced to slim
    # derivatives, and the raw multi-hundred-MB files thrown away; IMDB_DIR holds the slims.
    # Nothing here belongs in `gigaku backup` — unlike the word cache, it all re-downloads.
    IMDB_DIR: str = os.path.join(CACHE_DIR, "imdb")
    IMDB_MAX_AGE_DAYS: int = 14
    # For titles the dataset join misses, IMDb's unauthenticated suggestion endpoint resolves
    # a name to a tconst. Paced and capped, and every answer is cached on disk, because it is
    # someone else's free service.
    IMDB_SUGGEST: bool = True
    IMDB_SUGGEST_DELAY_MS: int = 400
    IMDB_SUGGEST_MAX: int = 500
    # Ranking by raw average puts 9.5-from-40-votes above 8.5-from-200k. The shrunk ("Bayesian")
    # mean fixes that: weight = (v/(v+m))·R + (m/(v+m))·C, for m minimum votes and C the prior.
    # A str with a parser, not two floats: _env coerces by the default's type and handles only
    # bool/int/str, so a float field would silently keep the raw environment string.
    IMDB_BAYES: str = "2000,7.0"

    # `gigaku clarity` asks Claude how obvious each i+1 card's unknown morph is from its
    # sentence, so that each word's my-learn card is its most guessable example rather than
    # merely its longest one. Scored through the `claude` CLI on the Claude Code
    # subscription (lib/anki/score.py) — no API key, and no Batch API either.
    # Opus, not Sonnet. Measured on the same rubric and comparable groups: Opus answers with
    # ~1.3k output tokens where Sonnet spent 15–31k thinking out loud, so it is several times
    # faster per call despite being the pricier model, and its scores sit lower and tighter
    # (medians 25–50, tops rarely past 70) instead of drifting up on weak blocks.
    CLARITY_MODEL: str = "opus"
    # Sentence length does one job here and only one: it is the **floor**. A sentence with
    # fewer than MIN_MORPHS morphs has no context to infer from, so it is dropped without a
    # call — mean clarity by length runs 0.02 · 0.13 · 0.24 · 0.28 · 0.27 · 0.37 · 0.38 for
    # 1..7 morphs against 0.46 at 8, and `--calibrate` picked 8 independently.
    #
    # Above the floor every alternate is judged: PER_WORD = 0 means no cap. It is tempting
    # to also *rank* candidates by length and keep the top few, and that is wrong — within
    # a word the clearest card is the longest sentence only 34% of the time, so a top-K by
    # length quietly discards the best example. Length says whether a sentence can carry
    # context at all; it does not say which of two long sentences carries more. Above the
    # floor a word has a median of 2 candidates but a mean of 5.6 and a tail out to 103,
    # and it is exactly the words with many long alternates — the ones where the choice
    # matters most — that a cap would truncate.
    #
    # 9 — a deliberate choice, above what the measurement alone would justify.
    #
    # What was measured: scoring 50 words with *no* floor and counting how often a card of
    # each length turned out to be its word's winner gives 0/19 at 1 morph, 0/45 at 2,
    # 1/94 at 3, then 7/119 at 4 and a flat 4–12% all the way up. So 4 is where a card
    # stops being unable to win — the floor below which asking is pointless.
    #
    # But "can win" is not "worth studying". A four-morph line of transcribed speech that
    # happens to reveal its word is still a scrap, and the queue is built out of whatever
    # this keeps. 9 buys a queue of sentences with something in them, at the price of the
    # words whose every example is short — those keep the old length-only rule.
    #
    # (Earlier values 5 and 8 came from asking the wrong question entirely — "how clear is
    # the average card of length L", a smooth curve with no cliff to read a floor off.)
    CLARITY_MIN_MORPHS: int = 9
    # The GERMAN floor, measured on its own corpus (two --calibrate runs, 2026-08-08,
    # bucket means agreeing to ±0.02): conversational-fragment transcripts — mean
    # clarity climbs 0.03→0.21 over 1..7 morphs and plateaus there, and the ja yield
    # bar (share ≥0.7) is never cleared, so `recommend()` proposes nothing. What IS
    # healthy is the thing the score exists for: the within-word pick holds at 75%
    # same-winner after the rubric re-anchor, the ja level. 5 is the knee where the
    # fragments end (en. / Kirche, Kirche. live below it); the ja 9 here would drop
    # most of the scoreable German cards for nothing.
    CLARITY_MIN_MORPHS_DE: int = 5
    CLARITY_PER_WORD: int = 0
    # Words per run, taken in study order (biggest alternate-group first) — the budget knob,
    # and it bounds *time* more than tokens: scoring runs about 5 minutes per 200 cards.
    # 0 means "every word", which is the whole backlog and several hours.
    CLARITY_WORDS: int = 500
    # Cards per invocation. Deliberately far bigger than an API request would want: each
    # `claude -p` drags Claude Code's ~24k-token harness along with it, so the invocation is
    # what needs amortising, not the rubric (see lib/anki/prompt.groups).
    #
    # Bounded above by the reply, and that bound tightened when the rubric grew the
    # unfinished-thought cap: output went from ~130 to ~240 tokens per card, so a 200-card
    # group that used to take 5 minutes started taking 13 and the next one blew a 30-minute
    # timeout outright. 150 keeps a call comfortably inside the hour score.py now allows.
    # Words are never split across groups (prompt.groups), and an oversized word is trimmed
    # to fit by select.candidates rather than cut in half.
    CLARITY_GROUP: int = 150
    # Cards per bucket for `--calibrate`'s stratified sample over am-all-morphs-count.
    CLARITY_SAMPLE: int = 60
    # `--loop` keeps taking budgets until the scope is empty. A failed pass is nearly
    # always a rate limit or a `claude` hiccup rather than something wrong with the work,
    # and everything scored so far is already on disk, so backing off beats surrendering
    # a job measured in hours. RETRIES counts *consecutive* failures.
    CLARITY_RETRIES: int = 3
    CLARITY_BACKOFF: int = 600

    def __post_init__(self):
        for f in fields(self):
            setattr(self, f.name, _env(f.name, getattr(self, f.name)))

    def override(self, *, validate=True, **kwargs):
        """Apply CLI flags (highest priority). Unknown names are a bug in lib/cli.py.

        ``validate`` is keyword-only so it can never be mistaken for a setting name: the
        word commands check the export directories they depend on, while a command like
        `titles` — which reads neither ~/Downloads nor the Anki dirs — must not be taken
        out by a stray GIGAKU_KNOWN_MORPHS_DIR in a shell profile.
        """
        known = {f.name for f in fields(self)}
        for name, value in kwargs.items():
            if value is None:
                continue  # flag not passed — keep env/default
            name = name.upper()
            assert name in known, f"unknown setting: {name}"
            setattr(self, name, value)
        if validate:
            self.validate()

    def validate(self):
        self.anki_min_counts()  # raises UserError on a malformed value
        for name in ("EXPORTED_FILES_DIR", "KNOWN_MORPHS_DIR"):
            path = getattr(self, name)
            if not os.path.isdir(path):
                raise UserError(f"{name} is not a directory: {path}")

    def anki_min_counts(self):
        parts = str(self.ANKI_MIN_COUNTS).replace(" ", "").split(",")
        try:
            known, learning = (int(p) for p in parts)
        except ValueError:
            raise UserError(
                f"ANKI_MIN_COUNTS must be two integers separated by a comma, "
                f"not {self.ANKI_MIN_COUNTS!r} (example: 7,9)"
            )
        return known, learning

    def hidden_langs(self):
        return {s.strip().lower() for s in self.PLOTS_HIDDEN_LANGS.split(",") if s.strip()}

    def save_langs(self):
        return [s.strip().lower() for s in self.KNOWN_MORPHS_SAVE_LANGS.split(",") if s.strip()]

    def ankimorphs_langs(self):
        return [s.strip().lower() for s in self.ANKIMORPHS_LANGS.split(",") if s.strip()]

    def report_langs(self):
        """Which languages `gigaku report` cards. Empty = everything `plots` doesn't hide."""
        named = [s.strip().lower() for s in self.REPORT_LANGS.split(",") if s.strip()]
        return named or []

    def mark_names(self):
        return [s.strip().lower() for s in self.TITLES_MARK.split(",") if s.strip()]

    def sublists(self):
        """The genre-node lists to walk, `su` always first so its order is the one kept."""
        names = [s.strip().lower() for s in self.TITLES_SUBLISTS.split(",") if s.strip()]
        return ["su"] + [n for n in names if n != "su"] if "su" in names else names

    def extra_ids(self):
        """TITLES_EXTRA_IDS as bare ids — a whole /title/<id> URL is accepted and reduced."""
        out = []
        for part in str(self.TITLES_EXTRA_IDS).replace(" ", "").split(","):
            digits = "".join(c for c in part.rsplit("/", 1)[-1].split("?")[0] if c.isdigit())
            if digits and digits not in out:
                out.append(digits)
        return out

    def hide_genres(self):
        return {s.strip().casefold() for s in self.TITLES_HIDE_GENRES.split(",") if s.strip()}

    def imdb_bayes(self):
        """(minimum_votes, prior_rating) for the shrunk mean — see IMDB_BAYES."""
        parts = str(self.IMDB_BAYES).replace(" ", "").split(",")
        try:
            minimum, prior = parts
            return int(minimum), float(prior)
        except ValueError:
            raise UserError(
                f"IMDB_BAYES must be an integer and a number separated by a comma, "
                f"not {self.IMDB_BAYES!r} (example: 2000,7.0)"
            )


settings = Settings()
