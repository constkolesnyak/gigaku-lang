# Gigaku — subtitles into Migaku

A three-file Chrome extension that hands the ripped subtitle pair for the Netflix episode you
just opened to **Migaku** — into its *Target Subtitles* and *Secondary Subtitles* slots — so an
episode starts with both tracks already loaded instead of two file pickers.

## Install (once)

1. `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → pick this folder (`chrome/gigaku`).
3. Open a Netflix episode you have subtitles for. The extension's toolbar badge says what
   happened:

   | badge | meaning |
   |-------|---------|
   | `…` grey | looking for a match |
   | `✓` green | **both tracks are loaded and on screen** — read back out of Migaku's own slots and overlay |
   | `–` grey | no subtitle pair for this episode |
   | `?` amber | Migaku's panel offered nothing to feed, so the files went to its drag-and-drop entry, which confirms nothing |
   | `H` amber | both tracks loaded, but Migaku is still not showing one of them |
   | `!` red | found the files, but Migaku wouldn't take them |
   | `M` amber | Migaku isn't running on this page |
   | `A` amber | Migaku has no toolbar here — its app window stayed shut |

**Click the icon to fix `M`, `A`, `!`, `?` or `H`**: if Migaku is switched off in Chrome, the click
switches it on (Chrome asks you to confirm — an extension may not do that silently) and
reloads the tab; if Migaku is on but never loaded into this page, the click reloads it; if
Migaku is right there, the click just tries the hand-off again.

Nothing else to configure: no server, no folder picker. Two permissions: `management`, to read
whether Migaku is enabled and — only on a click of this icon — to switch it on; and `tabs`, to
see whether Migaku's app window is already open before opening one.

## Migaku's app window

Migaku renders its toolbar only when `isLangReady && isAppWindowOpen && isDomainEnabled`, so
with its app window closed there is no toolbar, no Target/Secondary dropdowns and no file
inputs — nothing to feed and nothing to read back. That is the state the old green tick was
lying about. The flag is set by the app window's own page (`created(){ this.isAppWindowOpen =
true }`) and Migaku ships that page as a web-accessible resource, so when the toolbar is
missing this extension opens `pages/app-window/index.html` itself and waits for Migaku to come
up. Never a second one: Migaku's app window detects a duplicate and answers by setting
`isAppWindowOpen` back to false, so an already-open one is left alone.

In practice that page *is* "Migaku being on" — switching the extension off in Chrome is the
rarer case, and the only one that needs a click.

It gets a **window of its own, unfocused** — and that window is the point. Opening the same
page as a pinned background tab costs no animation and steals no focus, which is tempting on
macOS, where a new window is animated in *and* both focus changes are animated too (Migaku
pulls focus to it the moment it comes up — its own `requestShowAppWindow` defaults to
`focusWindow: true` — and then the hand-back animates again). But Chrome throttles timers in a
hidden tab and **Migaku does not survive that**: measured, it comes up broken. A second of
animation is the price of Migaku working; don't retry the tab.

Focus is handed back rather than fought for: for eight seconds, any time that one window takes
focus, the window playing the episode gets it straight back. Nothing else is countered, so
deliberately clicking into Migaku still works — it just has to be your click.

## Where the subtitles come from

`subs/` — inside this folder, which is the whole trick: a Chrome extension can `fetch()` its
own files, so the library needs no local server, no `file://` access and no re-import when it
grows. `gigaku subs` and `gigaku srt` write straight into it (`SRT_TARGET_DIR`, see
`lib/config.py`), as `subs/<Title>/<Title> - S01E03 - Primary.srt` (the language being learned)
plus `… - Secondary.srt` (its translation). The folder is gitignored — subtitles aren't code.

An extension can fetch a file but cannot *list* a directory, so `subs/index.json`
(`lib/subs/library.py`, rewritten on every rip) says what exists. Matching order:

1. **Netflix video id** — the number in `/watch/81237996`, recorded by `gigaku subs` for every
   episode it rips. Exact, and indifferent to how a title is spelled or localised.
2. **title + season + episode** from the same index (files converted by `gigaku srt`, which
   never sees an id) — the title comes from Netflix's own player metadata, read by
   `page-metadata.js`.
3. **the plain path** `subs/<Title>/<Title> - S01E03 - Primary.srt`, so files copied in by hand
   work even with no index.

Add files by hand and they resolve through step 3; run `gigaku subs`/`gigaku srt` and the index
is refreshed for you. If a brand-new file 404s, toggle the extension off/on in
`chrome://extensions` (Chrome cached the old resource list).

## How Migaku is fed

Read off Migaku's own bundle (v1.31.0.9, `assets/main-4ec4fdbb.js`) — both mechanisms are its
public-ish behaviour, nothing is monkey-patched:

* **Its two hidden file inputs** (`byoTargetSubsFileInput` / `byoSecondarySubsFileInput`, the
  "bring your own subs" pickers). Their `change` handlers register the file as a LOCAL subtitle
  *and select it into that slot* — so a `DataTransfer` + `change` puts Primary in Target and
  Secondary in Secondary with no language guessing. Migaku's UI root `#MigakuShadowDom` is an
  **open** shadow root, so the inputs are reachable. Each input is rendered *unconditionally*,
  which is why an input — not a dropdown — is what a slot needs.
* **A synthetic drop**, if those inputs can't be reached at all. Migaku's `DragAndDropZone`
  listens on `document` for `drop` and deliberately accepts a fabricated one
  (`evt instanceof CustomEvent && Array.isArray(evt.detail.types)`), reading `detail.files`.
  That path can't choose a slot, so the files get a language suffix (`.de.srt` / `.ru.srt`) and
  Migaku sorts them out by filename. Change `LANG_HINT` in `content.js` if the pair's languages
  change. It reports nothing back, so it never earns more than `?`.

## What the green tick means

That Migaku's two dropdowns were read back and name our two files — not that the files were
handed over. The difference is the whole point: Migaku's file handler is `read the FileList`
(*that* is what resets `input.value`, so the reset proves nothing) → `parse` → `select into the
slot`, and both of the last two give up silently. A Migaku that is switched off, or that hasn't
got the episode's track list yet, swallows the files and loads nothing — which is how a green
tick once ended up over an episode with no subtitles.

Each slot's dropdown label is bound to the player's `targetSubtitleData` /
`secondarySubtitleData`, i.e. to the track actually in use, so that label is the receipt. The
dropdowns live in Migaku's video-options popover, which mounts lazily — so `content.js` opens
it itself (a **non-bubbling** synthetic click on the toolbar button, found by its ClosedCaptions
icon path rather than its label so it survives an interface language change; non-bubbling
because the popover's click-away listener sits on `document` and would instantly re-close it),
feeds, verifies, and closes it again. A feed that doesn't verify is retried three times.

**The receipt is waited for after feeding, never required before it.** Migaku renders a slot's
dropdown only when `(its filtered options > 1) && targetSubtitleData !== undefined` — that is,
only once it already holds a usable track for the episode. On the episodes this extension is
*for* (no subtitles in the language being learned, which is why the track was ASR-ripped) it
shows "none available" instead, forever. Waiting for the dropdown before feeding therefore
refused the good path in exactly that case and fell through to the blind drop, which is a `?`
badge over an episode the file inputs would have loaded. Registering our file is what puts a
second option in that list, so feeding is what makes the dropdown — and the receipt — appear.

## Showing both tracks

Loaded is not shown, and that gap is a second way to end up staring at a bare episode. Migaku
hides a loaded track through three settings, and it keeps all three **per learning language**,
not per video — so one keypress or one study preset outlives by weeks the episode that set it,
and every episode after opens with both files in their slots and nothing on screen:

1. **`hideTargetSubtitles` / `hideSecondarySubtitles`** — the two "Always hide …" switches in
   the same panel we feed through. `w` and `shift+w` toggle them, `r` toggles both, and a
   mistyped hotkey is the usual way one gets left on.
2. **`playMode.type`.** Read off `PLAY_MODE_PRESETS`, five of Migaku's eight presets obscure a
   track on purpose — **primed listening hides both** (secondary until you pause); intensive
   reading, intensive listening, show-on-pause and one-T hide until their reveal condition
   fires. Only *default* and *intensive hybrid* show both outright.
3. **`playMode.custom.obscuring.{target,secondary}.effect`** — none / blur / hidden. The preset
   row can't answer this one, because it applies exactly when the preset *is* custom; that's
   why the row is only consulted when the custom panel isn't there to consult instead.

So the last thing every feed does is put all three back to showing both. Each is **read before
it is written**, so a Migaku already set the way we want it is never clicked — on almost every
episode this pass touches nothing — and each write is confirmed by a class Migaku binds to its
own store (`-toggled`, `--active`, `aria-checked`), never by the change we just made to the
control. That is the same distinction the feed makes between Migaku *reading* a file and Migaku
*loading* it.

The final receipt is Migaku's own: it puts `-hidden` on each track's list in the subtitle
overlay the moment anything takes it off screen, so that one reading covers all three settings
at once and needs no panel open. Only when it comes back clean does the badge go green; a track
still hidden is `H`, and a list that isn't there at all reads as *unknown*, never as hidden.

**Only German.** Everything here rips German-with-Russian pairs, so German is the only language
whose "show me both tracks" this extension has any business asserting — and because Migaku's
hide settings are per *learning language*, a pass that ignored that would reach into the
settings of a language it never fed a file to. Japanese is exactly that case: it runs Primed
Listening, a mode whose entire point is hiding both tracks until you pause, and flattening it
would be this extension breaking someone else's study. So the pass first reads
`data-mgk-lang-selected` — Migaku's own published attribute for the selected language, the one
its page bridge reads back — and does nothing at all unless it says `de`. Unreadable counts as
not ours. On another language the badge still goes green on a good feed, exactly as before:
the hand-off is what it answers for, and the hand-off worked.

What this deliberately does **not** do is fight for it mid-episode. A `w` pressed while
watching is a decision about this minute; the next episode is where "show me both tracks"
starts again. The pass runs once per episode, with the feed, and never watches.

## German audio

Everything here is watched dubbed, so every episode is put on its German soundtrack. Netflix's
own memory of that choice is per profile *and per title*, not absolute — a show it has never
played in German opens in the original language — which is exactly the gap this closes.

It goes through Netflix's **own player API** (`getAudioTrackList` / `getAudioTrack` /
`setAudioTrack`), never its on-screen menu, so there is nothing to break when Netflix
redesigns or when the interface is in another language. Two details the real player forced:

- **Read before write.** Re-selecting the track Netflix is *already* playing still costs an
  audible re-buffer, so an episode already in German is left completely alone — the pass says
  `already` and stops. This is the same discipline as the subtitle-visibility pass, for a
  reason you can hear.
- **Never `getAllPlayerSessionIds()[0]`.** After an episode swap Netflix keeps the previous
  session around and their order isn't stable, so the first entry can be a stale player whose
  setters silently no-op. The movie id from the URL is the discriminator. A *closed* session is
  only deprioritised, not skipped: measured on a live, playing episode,
  `isVideoPlayerClosedForSessionId` answered **true** for the one and only session, whose
  `getMovieId()` matched the URL — requiring an open one finds no player at all.

`PRIMARY` tracks win, so a German *audio description* track can never be picked over the plain
dub. A title Netflix doesn't offer in German is not a failure of anything and doesn't touch the
badge — the badge is a claim about the subtitle hand-off. The outcome goes to the console
(`[gigaku] audio → …`) with what it found and what it did.

Once per episode, never watched: switching to Japanese halfway through is a decision, and the
next episode is where German starts again.

## Files

| file | world | job |
|------|-------|-----|
| `background.js` | service worker | index lookup, reads the SRTs, sets the badge |
| `content.js` | isolated | watches the URL, feeds Migaku, reports the outcome |
| `page-metadata.js` | MAIN | Netflix's page globals — `videoMetadata` (title/season/episode) and the player's audio track — the things an isolated content script can't reach |

Netflix swaps episodes without reloading the page, so `content.js` polls `location` and re-runs
on every change, remembering the last episode it fed.

## Icon

The kokeshi dolls in `icons/` (16/32/48/128, resized from the 512px original) are
[Japan](https://www.flaticon.com/free-icon/japan_14932524) by
[MEDZ](https://www.flaticon.com/authors/medz) from [Flaticon](https://www.flaticon.com/)
— their free licence wants that credit kept.
