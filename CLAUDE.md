# Gigaku

Three things, one CLI:
1. **Vocabulary tracking** — `import` / `plots` / `kanji` / `anki` / `words`: pulls the words you know from Language Reactor + Migaku, caches them, and draws the history.
2. **Netflix subtitle ripping** — `gigaku subs` autonomously exports a whole Netflix season's subtitles via the Language Reactor extension, as aligned Primary/Secondary `.srt` pairs. `gigaku srt` is the manual counterpart (xlsx already in ~/Downloads).
3. **Mouse → Apple TV volume + Netflix transport** — a launchd daemon turns the physical mouse into the remote: wheel = Apple TV volume (falling back to the Samsung TV's own remote when the Apple TV can't be reached), left button = Netflix play/pause, right button = Netflix fullscreen.

(The vocabulary half was rewritten on the standard library when it was merged in on 2026-07-14: gigaku has **four** dependencies, one of which installs nothing new, and suggestions that reach for pydantic/pandas/plotly/rich belong to a different project.)

## Run

```bash
gigaku plots                              # vocabulary history → self-contained HTML, opens it
gigaku publish                            # same page → PUBLISH_DIR, served tailnet-only
gigaku backup                             # snapshot cache + Migaku + AnkiMorphs → private git repo
gigaku report --send                      # the week's progress → Telegram, one chart card per language
gigaku import                             # LR/Migaku exports + Chrome + AnkiMorphs → word cache
gigaku kanji | head                       # kanji by known-word count (TSV on stdout)
gigaku anki --anki-min-counts 7,9 | pbcopy
gigaku words --lang de --stage known
gigaku clarity --calibrate                # what morph threshold is worth paying for (TSV)
gigaku clarity --words 120 --loop         # rate every i+1 alternate, a budget at a time
gigaku clarity --restale --words 50       # re-judge what an older rubric scored, whole words
gigaku info                               # settings + what's in the cache
gigaku titles                             # rip a Netflix browse gallery, rank it by IMDb rating
gigaku titles --probe | jq .              # what the page offers, when a Netflix build breaks the rip
gigaku subs                               # rip current Netflix season's subs → chrome/gigaku/subs/
                                          #   (tab on /watch/… or just /title/… — it starts the player)
gigaku subs 3-12                          # only part of it (3- to the end, -3 from the start)
gigaku subs --no-translate                # …and let LR machine-translate, as it used to
gigaku spell                              # proofread the ripped German (ASR spelling only)
gigaku spell 'Blue Lock' --dry-run        #   what the German dictionary doubts — free
gigaku spell 'Blue Lock' --show           #   every correction on record; --revert undoes them
gigaku translate                          # gloss ripped German → Russian for every episode
gigaku translate 'Blue Lock' --episodes 5-6 --redo
gigaku srt 'Dark' --season 2              # convert LR Excel subs already in ~/Downloads (same target)
gigaku sound                              # mouse-wheel → Apple TV volume daemon (usually via launchd)
uv run python -m lib.volume               #   …which device a step reaches right now (one up, one down)
uv run python -m lib.samsung.control      #   pair with the TV / check the fallback on its own
uv run pytest
```

Every `Settings` field (lib/config.py) reads `GIGAKU_<NAME>` and is overridden by the matching `--flag`. **Status/progress goes to stderr, never stdout** — `kanji`/`anki`/`words` are piped, and chatter on stdout would land in the TSV or the clipboard. Use `config.note()`, not `print()`.

## Architecture

Everything is grouped into domain subpackages so new features have an obvious shelf. Domain
modules are imported **lazily** (inside each `cli` `case`, or at call time), so `gigaku srt`
never loads the Apple TV / pyobjc stack and `gigaku sound` never loads openpyxl.

**`lib/` root — shared by every domain**
- `cli.py` — argparse entry point; every command dispatched from `_run()`, which imports its domain module lazily.
- `config.py` — Apple TV constants (mDNS/MAC/storage), Netflix constants, `Settings` (env + flags), `UserError`, `note()`.
- `pages.py` — the shared layer under every self-contained HTML page (plots, the report card and week page, titles): `render()` (sentinel substitution + the `</`-escape), `json_payload()`, the **one categorical palette** (pages read `DATA.palette`, the report card indexes the dark row — it used to exist twice with nothing pinning the copies equal), and `BASE_CSS` (theme variables + the `[hidden]` fix, inlined via `__BASE_CSS__` so pages stay self-contained). Chart JS stays per page **on purpose** — a page readable top to bottom in one file beats deduplicated JavaScript. `tests/test_pages_smoke.py` renders each page with fixture data and executes its JS in headless Chrome (`--dump-dom`; Chrome writes and does not exit — measured, same as the report renderer — so the output is the signal and the process is killed).
- `telegram.py` — the transport only: `credentials()` (env → `TELEGRAM_ENV_FILE`, deliberately not Settings) and `tg_call()`. What to say and when to stay silent is each caller's judgement (`report` posts weekly, `backup` alerts on failure).
- `claude.py` — the **`claude -p` transport**, same shape as `telegram.py`: `executable()`, `ask(text, system, model)` → `(reply, usage, cost)`, `describe(usage)`. Shared by `gigaku clarity` (`lib/anki/score.py`) and the subtitle glosser (`lib/subs/translate.py`), so one place knows how a Claude Code subscription is spoken to — no API key, no `anthropic` dependency. The three measured facts every caller is shaped by live here: each invocation drags Claude Code's own **~24k-token harness** along irreducibly (so the *invocation* is what needs amortising — requests are hundreds of items, not dozens), there is **no server-side schema** (so every contract is plain lines matched on an echoed id, and a short reply is re-asked rather than trusted), and cost per call is **erratic** (1.8k–35k output tokens across eight identically-shaped calls), hence per-run budgets and an hour-long timeout.

**Where the rest of these notes live.** This file is the map: what the commands are, the shared
layer under every domain, the host glue, the error types, the dependencies. Each domain's
*measured* detail — the failures that shaped it, what was tried and rejected, the numbers that
settled an argument — lives in a `CLAUDE.md` **beside its code**, which is loaded automatically
the moment a file in that directory is opened. **Read the domain file before changing anything
in it**: almost every paragraph there is a trap that already cost something, and the entries
below are only enough to tell you which file to open. **New findings go into the domain's file,
not into this one** — the root grew to 150k characters once, past the point where it was loaded
at all, and it stays a map by being kept one.

**`lib/vocab/` — vocabulary tracking (`import`/`plots`/`kanji`/`anki`/`words`/`publish`/`backup`/`report`)**
`words.py` (the store, `Word`, `merge`), `lr.py`, `migaku.py` (Chrome's IndexedDB, **two** origins
and the freshest wins by the data's own clock), `ankimorphs.py` (the third word source; **nothing
is filtered — the contract is that the counts agree**, and a morph Anki cannot date carries the
year 1), `kanji.py`, `plots.py` (hand-written SVG, self-contained HTML), `publish.py` (into
PUBLISH_DIR, tailnet-only — **the Mac must be the one that gathers**), `backup.py` (daily
off-site git snapshot; stage → gate → swap, and it alerts Telegram on failed/refused/stale),
`report.py` (the week just gone, one PNG + caption to Telegram). The word cache
(`~/Library/Caches/gigaku/words.json`) is the **only** record of history — the exports are deleted
after import and neither source keeps a per-day one, which is the whole reason `backup` exists.
→ **`lib/vocab/CLAUDE.md`**

**`lib/subs/` — Netflix subtitle ripping + SRT (`subs`/`srt`/`spell`/`translate`)**
`subs.py` drives Language Reactor's own in-page controller over AppleScript — no cursor
automation, no focus stealing (page visibility is spoofed), and **the video is never played**,
not even to generate ASR. `excel_to_srt.py` converts LR's xlsx and **reads the exported text**
rather than trusting LR's track label; `translate.py` glosses the Secondary with Opus instead of
letting LR machine-translate it, to an interlinear-gloss rubric that is the user's requirement;
`spell.py`/`spell_prompt.py` proofread the ASR German **before** the gloss reads it — every
line, matched on an echoed id, with a per-series name ledger because one character spelled five
ways is the corpus's commonest defect; `library.py`, `episode_range.py`, `srt.py`, `naming.py`
are the pure halves.
→ **`lib/subs/CLAUDE.md`** (the whole `gigaku subs` flow, the rubric, and the "tab was closed"
runbook)

**`lib/netflix/` — browse-gallery ripping, IMDb ratings, the player remote (`titles`)**
`gallery.py` drives Netflix's **own falcor client** (never a hand-built request), `imdb.py` joins
IMDb's own datasets with a transparent match ladder, `watched.py` reads the Watched Marker
extension's own export, `markers.py` takes Anime/Korean from Netflix's per-title genre rows,
`store.py` + `titles.py` + `titles_page.html` are the record shape and the page. `remote.py` is
the mouse buttons' half of the player — it only *posts a key* (F17/F18), because fullscreen can
only be entered from inside a real user gesture, so the behaviour lives in the extension.
→ **`lib/netflix/CLAUDE.md`**

**`lib/anki/` — context-clarity scoring for i+1 cards (`clarity`)**
`My Clarity`, 0.00–1.00: how obvious a card's unknown morph is from its sentence alone, scored
through the `claude` CLI. The add-on's pick is the word's **highest `My Clarity`, full stop**; a
word's cards are rated against each other in one block, and a word is scored whole or not at all.
→ **`lib/anki/CLAUDE.md`**

**`lib/platform/` — macOS host glue, shared and domain-agnostic**
- `applescript.py` — `Foundation.NSAppleScript` wrapper (`run()`, `AppleScriptError`; transient Apple-event errors retried inside `run`).
- `chrome.py` — `exec_js_on_extension(url_substring, js)` (run JS on the first Chrome tab whose URL matches, via AppleScript — the *isolated* world), plus the shared driving primitives: `SPOOF_VIS`, `PageError`/`TabClosed`, `wait_progress()` and `Page(url_match, prelude=…)` with `.dom()` / `.main()` (the main-world hop: inject a `<script>`, read its `JSON.stringify(o)` back out of a hidden node) / `.tab()` / `.navigate()` (the document-turnover barrier) / `.goto()` / `.closed()`. `lib/netflix/gallery.py` is built on `Page`; `subs.py` predates it and keeps its own thin wrappers over the same ideas, but **`SPOOF_VIS` lives here and `subs.py` imports it** — the visibility spoof is hard-won and must exist once.
- `power.py` — `DisplayWakeLock` context manager: an IOKit `PreventUserIdleDisplaySleep` assertion via ctypes (no `caffeinate`/subprocess).
- `net.py` — finding a device on the LAN without mDNS: `ip_for_mac()` / `ips_by_mac()` (the ARP table, parsed pure so it can be tested — macOS prints `00:7d:…` as `0:7d:…`, so both sides are normalised) and `local_ip()`/`subnet_hosts()` for a /24 scan. Both TVs are found this way and for the same reason: **mDNS wedges for hours here and every IP written down goes stale** (the TV's hardcoded IP was long stale). It used to be two copies inside `lib/appletv`; one helper is what keeps them in step.

**`lib/appletv/` — Apple TV control (imported as `lib.appletv`)**
- `control.py` — pyatv/Companion volume control for the wheel (`connect()`, `volume_up/down` HID). Resolves the Apple TV IP: mDNS → ARP-by-MAC → published cache. The package `__init__` re-exports the public API, so callers keep `from lib import appletv`. **How hard it looks before calling the device absent is the caller's** (`scans`/`scan_timeout`, and `MDNS_TIMEOUT` on the name): measured 2026-08-14 with the Apple TV off, the old ladder spent **15s on three failed name resolutions and 15s on three empty scans** — half a minute to prove an absence, all of it in front of whatever was waiting. mDNS is now asked **once** and briefly, because it fails *structurally* here (client isolation, for hours at a time) and the two rungs under it are exact and instant; `lib/volume.py` asks for a single short scan, which is what makes the fallback land in ~3s instead of ~30.

**`lib/samsung/` — the Samsung TV's own remote (imported as `lib.samsung`)**
- `control.py` — the modern Tizen remote (2016+, here a GQ55Q80DATXZG): token-authenticated WebSocket on `wss://<ip>:8002`, one JSON `ms.remote.control`/`SendRemoteKey` per key. None of the old H-series encryption applies. Three measured facts shape it. **(1) The TV never answers a WebSocket ping** — with aiohttp's `heartbeat=10` the channel is killed as unanswered after ~30s (code 1006) every time, while an unpinged idle channel was still open after 5½ minutes. So there is no heartbeat and a dead channel is discovered the only other way there is: a send that fails, which the caller answers by reconnecting. **(2) With a stored token the handshake is 0.04s and silent** — no dialog, no on-screen trace — which is what makes reconnect-on-failure the whole recovery strategy. The token (`.tv_remote_token`, gitignored) is the one the pre-Apple-TV daemon paired in 2026-06, so nothing needed re-accepting. **(3) The TV is wired**, so it needs none of the lan_bypass ARP pin the Apple TV depends on — that isolation is per-SSID and this TV isn't on the Wi-Fi. Finding it is `net.ip_for_mac(SAMSUNG_MAC)` → the IP we last published → a concurrent scan of the /24 for port 8001, **every candidate confirmed by the device's own `GET /api/v2/`** (a stale cache pointing at somebody's laptop must not become "the TV"); a lone Samsung whose MAC we don't know is accepted *and named in the log*, because a replaced TV is a likelier story than a second one. `python -m lib.samsung.control` is the selftest and the way to accept the dialog deliberately.

**Top level**
- `lib/volume.py` — **where a wheel notch goes**: the Apple TV, or the TV itself when the Apple TV can't be reached. The two are the same speaker — the Apple TV has no volume of its own, it relays the key to this TV over HDMI-CEC — so the fallback is not a degraded mode, it is the same sound by a shorter path, and all that is lost is which box's volume bar appears. That fact is what licenses every rule here. **A failure falls over inside the same notch** (the step that discovers the Apple TV is gone is sent to the TV, not dropped). **A live connection gets one reconnect; a failed connect gets none** — a send failing on a held connection is a stale socket, a connect that just failed will fail again, and retrying it only doubles the stall in front of the fallback. **Coming back is a background job**: while the fallback is active a task probes every `PROBE_EVERY` (60s) and *parks a live connection* which the next step adopts for free, so the way back never costs a notch. **The send path is impatient and the probe is patient**, which is the asymmetry that makes it feel instant: being wrong on the send path costs a step played through the TV instead of the Apple TV, i.e. nothing audible, so it asks once and briefly; the probe, with nobody waiting, asks with the full patience. Pacing is the *target's* (`gap`) — the Apple TV's 0.20s is its CEC pipeline's measured ceiling, the TV applies its own keys and takes 0.10s. It lives here rather than in the daemon so the policy is testable at all (`tests/test_volume.py` drives it against fake devices; `scroll_volume.py` can't be imported without an event tap).
- `scripts/scroll_volume.py` — the mouse daemon: a `CGEventTap` on scroll/click events driving `lib/volume.py` (wheel → volume, Apple TV or TV) and `lib/netflix/remote.py` (buttons → play/pause, fullscreen). **A script, not a package module**: the launchd stub `execv`s `.venv/bin/python3 scripts/scroll_volume.py` from the repo root, and `gigaku sound` loads the same file by path. It keeps only the accumulator and the pacing, and **paces from when the key was delivered, not from when the send began** — normally a 10ms difference, but on the notch that discovers a target is gone the send takes seconds, and a start-to-start clock let the next notch fire the instant it returned (measured: two keys 23ms apart on the notch that fell over to the TV).
- `scripts/pair_appletv.py` — one-time Apple TV pairing (writes pyatv FileStorage at `.atv_storage`).
- `tests/` — pytest over the pure logic (merge, cache adoption, importers, kanji, plot series, SRT), with real LR/Migaku export fixtures in `tests/data/`.
- `launchd/GigakuSound.app` + `launchd/com.gigaku.sound.plist` — launchd agent for the wheel daemon. Only the bundle's `Info.plist` is tracked; the stub binary is built locally from `launchd/GigakuSound.m` (`mkdir -p launchd/GigakuSound.app/Contents/MacOS`, `clang`, then ad-hoc `codesign` — the README has the exact lines) and gitignored, because the Accessibility grant is bound to the signature of the exact binary on this machine.
- `scripts/lan_bypass.*` — root LaunchDaemon that keeps the Apple TV reachable under Wi-Fi client isolation (see troubleshooting below).
- `freqdeck/` — **the `Frequency 🇩🇪` deck: the head of `freq/de-words.tsv` turned into 🇩🇪 German notes whose example sentence and its audio are *generated*** (built to 998 notes, 2026-08-27). Six cached stages in `deck.py` (`sentences` → `check` → `translate` → `tts` → `wordaudio` → `apkg`, then `media`), then `anki_import.py` inside Anki, `definitions.py` outside it, `recalc.py`, `audit.py`/`fix_audit.py`. What is worth knowing before touching it, each measured and each written up in `freqdeck/README.md`: the sentence rubric is **one test** (blank the target, then try its antonym) with every other rule as a consequence, and every request carries the whole target list as "words he does not know"; the pick among three candidates is made by the **existing** clarity rubric `lib/anki/prompt_de.py` plus a vocabulary flag that is the **intersection of two witnesses** (in `de-words.tsv` AND not in `gigaku words --stage known` — either alone is noise); the card's audio must land in **collection.media**, since the 🇩🇪 template plays `[audio:]` from the webview; media names are **lowercase** because Anki lowercases what it stores while the note keeps the capital; **Commons rate-limits by count, not pace** (~12–16 requests then a 600s ban, ~60 files/hour whatever the delay — 987 recordings took 16 hours), so `wordaudio` runs to completion *first* and a word is TTS'd only where Commons genuinely has nothing (the user's rule: no placeholder assets); and the Anki **UI is not used** — `definitions.py` reproduces the 💣 over AnkiConnect (same prompt, model, parameters and field format) because the UI path hung on a Commons-429 modal, blocked the main thread per chunk, and in All-In-One mode moved cards to Study 🇩🇪. `audit.py` is the closing gate: mechanical checks over every field plus an Opus review of word+sentence+definition+translation together (959/999 clean first pass; the 40 findings were fixed by `fix_audit.py`, which rewrites the caches too, and re-reviewed). `out/` is gitignored; `tests/test_freqdeck.py` pins the pure parts.
- `chrome/gigaku/` — a small MV3 Chrome extension (load-unpacked) that hands the ripped SRT pair for the Netflix episode you just opened to **Migaku**, into its *Target Subtitles* / *Secondary Subtitles* slots, picks the German soundtrack through Netflix's own player API, and un-hides both tracks — all once per episode, never watched. Its badge is a **claim that is earned**: `✓` means Migaku's own dropdowns were read back and name our files. The library lives at `chrome/gigaku/subs/`, *inside* the extension, because an extension can `fetch()` its own files but cannot list a directory (hence `lib/subs/library.py`'s `index.json`). → **`chrome/gigaku/CLAUDE.md`** (engineering notes) and `chrome/gigaku/README.md` (the user-facing half).
- `.claude/skills/sentence-mining/` — **a German YouTube video or playlist → 🇩🇪 German cards**, the only part of `.claude/` this repo versions (`.gitignore` un-ignores `skills/`). `fetch.py` takes the transcript from **YouTube's own `de-orig` captions** — punctuated, capitalised and word-timed, so there is no ASR step, no key and no cost — and `generate_media.py` cuts the sentence out of the m4a with ffmpeg, so the card's audio is the speaker's actual voice. The one idea worth knowing is that a word has **three** fates, not two: known (Migaku, and *only* Migaku — `migaku/de.csv` from `gigaku backup`, because the word cache carries no provenance and 345 of its German words are not Migaku's), **graded** (not in Migaku but in the top 1500 of `freq/de-yt.tsv`, or vouched for by the cache as a second witness), and taught. Measured over one playlist: **516 grade-words against ~650 real ones** — carding them would have spent two thirds of the deck on `eigentlich` and `bekommen`, so they go to `grade-me.tsv` to be marked in Migaku instead, and count as known for the i-level (counting them unknown makes every sentence i+3). What the frequency lists cannot judge goes to `triage.py`, one `claude -p` per 150 words. Cards are **finished by `freqdeck/definitions.py --deck`** — the same 💣, model and voice as `Frequency 🇩🇪` — so the two decks read alike. → **`.claude/skills/sentence-mining/SKILL.md`** and its `references/`.
- `anki/` — **two** Anki add-ons, both symlinked into Anki's `addons21/` by `anki/link.sh`: `anki/gigaku/` (ours, the CLI-side rules mirror — `rules.py`/`queries.py`/`trim_rules.py` import cleanly outside Anki and `tests/test_addon_rules.py` pins them against `lib/anki/`) and `anki/MvJ Japanese/` (**not in this repository**: third-party code with no redistribution licence. It lives in a private repo and is symlinked in at this path, which `.gitignore` covers, so `link.sh` and the drift test in `tests/test_addon_config.py` see it as before). **German IS the Japanese setup since 2026-08-13** — one template, one tag convention, one importer. → **`anki/CLAUDE.md`**

## Error types

- `AppleScriptError` (`.error_number`) — AppleScript failure. Transient ones (`AppleEvent timed out` -1712, connection -609/-600) are retried inside `applescript.run`; a persistent one is caught by the load/episode retry loops (reload-and-retry), so it no longer crashes a run. Config errors (app not running, "Allow JavaScript from Apple Events" off) still surface.
- `ExportError` (`lib/subs/subs.py`) — a recoverable step failure (track not found/won't verify, stalled load/translations, download never appeared, incomplete file). Triggers a reload/retry; an episode that exhausts its retries is skipped, not fatal.
- `_Aborted` (`lib/subs/subs.py`) — the Netflix tab was **closed** → the run ends; re-run to resume. It carries a **`reason`**, and the ending prints it, because the ending used to *guess*: every stop printed "Stopped (Ctrl-C or tab closed)" whether or not either had happened. Being hidden/backgrounded is NOT an abort (visibility is spoofed), and neither is a tab that merely **left the watch page** (`lib/subs/CLAUDE.md`).
- `PageError` / `TabClosed` (`lib/platform/chrome.py`) — the domain-agnostic pair behind `Page`: recoverable step failure (retry/reload) vs. the tab is gone (stop). `Page(..., error=…)` lets a caller substitute its own recoverable type, which is how `subs.ExportError` keeps working unchanged.
- `UserError` (`lib/config.py`) — anything the user can fix (bad flag, no exports, empty cache, Migaku not in Chrome). `lib/cli.py` prints it to stderr without a traceback and exits 1. Raise it instead of asserting whenever the cause is *their* input, not a bug.

## Troubleshooting: scroll-wheel volume suddenly dead

Since 2026-08-14 this is **not silence any more**: `Apple TV unreachable` is followed by
`volume → TV` and the wheel keeps working through the Samsung's own remote
(`lib/volume.py`), which is the same speaker. So the symptom to chase is now "the wheel
does nothing at all" or a log that says `TV unreachable` too; a wheel that works but shows
the *TV's* volume bar instead of the Apple TV's is the fallback doing its job, and it
returns on its own within a minute of the Apple TV coming back. The diagnosis below is
still the reason the Apple TV went missing.

If `scripts/scroll_volume.py` stops controlling volume and `/tmp/gigaku-sound.log` spams
`Apple TV unreachable`, the cause is **per-SSID Wi-Fi client isolation** at the
router — the Mac can reach the gateway but no LAN peer (`ping` to the Apple TV
works, but TCP gives instant `No route to host`). It is NOT the Apple TV, not
gigaku, not the Mac's MAC/VPN/firewall, and **must not be "fixed" by toggling
Wi-Fi**. Full runbook: **`scripts/lan_bypass.README.md`**.

The shipped fix (already installed): a root LaunchDaemon (`scripts/lan_bypass.sh`
+ `com.gigaku.lanbypass.plist`) pins the Apple TV's ARP entry to the **gateway's
MAC**, so its traffic hairpins through the router (a path the AP allows), and
publishes its IP to `/tmp/gigaku-atv-ip`. `lib/appletv/control.py` then connects by
**unicast host-scan** of that IP, Companion-only (pyatv's multicast scan is
unreliable under isolation).

**Second failure mode: mDNS dies too.** The AP's isolation also wedges multicast,
so `Living-Room-….local` can stop resolving for hours even though the Apple TV
sits in the ARP table. Fixed: resolution no longer relies on mDNS — both
`lan_bypass.sh` and `lib/appletv/control.py` fall back to finding the IP by the Apple TV's
fixed **hardware MAC** (`config.APPLETV_MAC`, the hex in the hostname) in the ARP
table. Order everywhere: mDNS → ARP-by-MAC → published cache. Quick check:
`arp -an | grep -i 02:ad:be` should show the Apple TV. Gotcha: a correct-looking
pin can still fail until the kernel's cached `EHOSTUNREACH` is flushed with
`arp -d` + `arp -s` (the daemon self-heals this every 30 s). Note: a sandboxed
shell may fail these connects even when the machine is fine — trust the daemon log.

## Dependencies

Four lines in `pyproject.toml`, three actual installs, and it stays that way. Everything the vocabulary side needs — JSON, CSV, SQLite, zlib, HTTP-free HTML, the charts — is stdlib or hand-written; a new dependency there should be argued for, not assumed. `gigaku clarity` nearly made it four for real (`anthropic`) and then didn't need to: it scores through the `claude` CLI on the Claude Code subscription, so the whole LLM surface is `subprocess` + `json`.

- `pyobjc-framework-quartz` — `Foundation` (NSAppleScript) + `Quartz` (CGEventTap for the wheel).
- `pyatv` — Apple TV Companion control.
- `openpyxl` — read the LR xlsx export.
- `aiohttp` — **declared, not added**: pyatv already installs and requires it, so this pins nothing new. `lib/samsung` uses it directly for the TV's `wss` remote channel; the alternatives were a genuinely new install (`websocket-client`, what the retired `lib/remote.py` used) or hand-rolled RFC 6455 framing. Using it undeclared would have been the actual sin.
- The `claude` CLI on PATH — `gigaku clarity`, `gigaku translate`, `gigaku spell`, and `gigaku subs` unless `--no-translate` — and AnkiConnect reachable with Anki running (`clarity` only).
- (dev) `pytest`.
- Chrome "Allow JavaScript from Apple Events" enabled (View ▸ Developer), and the Chrome binary itself (`CHROME_APP`) — `gigaku report` runs it **headless** to turn its cards into PNGs, which is what keeps the chart drawing at zero dependencies. Python ≥3.14, managed by uv.
