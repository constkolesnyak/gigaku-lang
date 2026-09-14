/* Isolated-world half: notice which episode is playing, get its SRT pair from the service
 * worker, and hand both files to Migaku — then check that Migaku actually took them.
 *
 * How Migaku takes subtitles (read off its bundle, v1.31.0.9 assets/main-4ec4fdbb.js):
 *
 *   1. Its `SubtitleSettings` component renders two hidden file inputs — the "bring your own
 *      subs" pickers behind the Target Subtitles and Secondary Subtitles dropdowns. Their
 *      change handlers read `e.target.files`, register them as LOCAL subtitles and then select
 *      the loaded track *into that slot*. So a DataTransfer + change event puts each file
 *      exactly where it belongs, with no language guessing. Migaku's UI root
 *      (`#MigakuShadowDom`) is `attachShadow({mode:"open"})`, so the inputs are reachable.
 *
 *   2. If those inputs aren't mounted, Migaku's `DragAndDropZone` also listens on `document`
 *      for `drop` and explicitly accepts a *synthetic* one:
 *      `evt instanceof CustomEvent && Array.isArray(evt.detail.types)` → it reads
 *      `detail.files`. That path can't pick the slot, so the files are renamed with a language
 *      suffix (`.de.srt` / `.ru.srt`) — Migaku derives a language from the filename.
 *
 * WHAT COUNTS AS SUCCESS, and why it isn't what it used to be. The old code took Migaku
 * resetting `input.value` as the receipt. It isn't one: Migaku's handler reads
 * `files = await readFiles(evt)` — and it is *that* read which resets `input.value`, before
 * anything is parsed or selected. Both later steps give up silently (`if (!subs.length)
 * return`, and the "select it into the slot" step polls 50×100ms and then stops). A Migaku
 * that is switched off, or that hasn't got the episode's track list yet, resets the input and
 * loads nothing — which is exactly how a green ✓ ended up over an episode with no subtitles.
 *
 * The real receipt is one step further in: each slot's dropdown label is bound to
 * `targetSubtitleData` / `secondarySubtitleData`, i.e. to the track the player actually holds.
 * So we read that label (vue-multiselect's `.multiselect__single`) before and after feeding,
 * and only call it loaded once it names our file. Nothing to feed, or nothing that answers →
 * the badge says so instead of going green.
 *
 * AND THEN: loaded is not shown. Migaku hides a loaded track through settings it keeps per
 * learning language, so a study preset or a stray "w" outlives its episode by weeks and the
 * next one opens with both files in place and nothing on screen. The feed therefore ends by
 * putting those settings back to showing both — read first, so an already-correct Migaku is
 * never clicked — and the badge only goes green once Migaku's overlay stops saying `-hidden`.
 * See "making both tracks visible" below.
 *
 * That receipt is waited for AFTER feeding and never required before it. Migaku renders a
 * dropdown only once it already has a usable track for the episode, so on the episodes this
 * extension is for — no subtitles in the language being learned, which is why the track was
 * ASR-ripped — the dropdown is absent until our own file becomes that track. Feeding is what
 * makes the receipt printable; only the file inputs are a precondition (see `slots`).
 *
 * Those dropdowns live in Migaku's video-options popover, which mounts lazily — so if it is
 * closed we open it ourselves (a non-bubbling synthetic click on its toolbar button; the
 * popover closes itself on any document click, so the click must not bubble), feed, and close
 * it again.
 */
(function () {
  'use strict';

  const CHANNEL = 'gigaku-subs-channel';
  const POLL_MS = 500;          // Netflix swaps episodes without a reload — watch the URL
  const READY_MS = 60000;       // how long to wait for Migaku's UI to appear at all
  const TOOLBAR_MS = 45000;     // …for its toolbar, which waits on its app window booting
  const PANEL_MS = 10000;       // …for its video-options panel, once we've asked for it
  const TRACKS_MS = 30000;      // …and for the episode's track list, which it fetches per video
  const TALK_MS = 10000;        // a service-worker round trip that goes quiet
  const CONSUMED_MS = 5000;     // how long to wait for Migaku to read a fed file
  const SELECTED_MS = 12000;    // …and to put the parsed track in the slot (it polls for 5s)
  const SETTING_MS = 4000;      // …for a setting we flipped to come back in Migaku's own class
  const SCREEN_MS = 6000;       // …and for the overlay to stop hiding the tracks
  const AUDIO_MS = 25000;       // …for Netflix's player to boot far enough to answer about audio
  const AUDIO_SETTLE_MS = 1500; // …and for a track switch to show up as the current one
  const ATTEMPTS = 3;           // whole feeds, not retries of a step
  const BETWEEN_MS = 2000;

  // Only used by the drop fallback, which assigns slots by language rather than by input:
  // Primary is the language being learned, Secondary its translation.
  const LANG_HINT = { primary: 'de', secondary: 'ru' };

  // Migaku's ClosedCaptions icon — the video-options popover's trigger. Matching the icon's
  // path rather than the button's label keeps this independent of Migaku's interface language.
  const CC_ICON = 'M1.75 7A4.25 4.25 0 0 1 6 2.75h12';

  // Migaku's play-mode row, in the order it builds it (`PLAY_MODE_PRESETS` and the options
  // list in main-*.js): default, primed listening, intensive reading, intensive listening,
  // intensive hybrid, show on pause, one-T, custom. Five of the eight obscure a track on
  // purpose; only these two show both outright.
  const MODE_COUNT = 8;
  const DEFAULT_MODE = 0;
  const OBSCURING_MODES = new Set([1, 2, 3, 5, 6]);

  // …and the Default mode's own icon, so the click is aimed by what the button shows rather
  // than by its place in a row Migaku is free to reorder. Same reasoning as CC_ICON.
  const DEFAULT_MODE_ICON = 'M109.448 276.499c0-15.906 12.895-28.802 28.802-28.802h210.717';

  // The one learning language this extension is allowed to change display settings for, and
  // the attribute Migaku publishes it on (`ATTRS.LANG_SELECTED` in its own constants, read
  // back by its own page bridge — a purpose-built hook, not a class that happens to be there).
  //
  // Everything here rips German-with-Russian pairs, so German is the only language whose
  // "show me both tracks" this extension has any business asserting. Migaku's hide settings
  // are per learning language, so a pass that ignored this would reach into the settings of a
  // language it never fed a file to — and flatten a deliberate study mode there. Japanese is
  // exactly that case: it runs Primed Listening, which hides both tracks on purpose.
  //
  // Unreadable counts as not ours: no attribute, no pass.
  const SHOW_FOR_LANG = 'de';
  const LANG_ATTR = 'data-mgk-lang-selected';

  let currentId = null;
  let token = 0;                // bumped on every episode change; stale runs bail out

  // ── talking to the page and the service worker ─────────────────────────────

  function watchId() {
    const m = location.pathname.match(/\/watch\/(\d+)/);
    return m ? m[1] : null;
  }

  /**
   * Ask the MAIN-world half something and keep asking until it answers. Both questions it
   * takes — the episode's metadata and Netflix's audio track — are reads of a page global that
   * exists only once the player has booted, so "no answer yet" is the normal opening state and
   * a falsy `data` means *ask again*, never *no*.
   */
  function askPage(cmd, extra, timeoutMs) {
    return new Promise((resolve) => {
      const nonce = Math.random().toString(36).slice(2);
      const deadline = Date.now() + timeoutMs;
      let timer = null;

      const onMessage = (ev) => {
        const d = ev.data;
        if (ev.source !== window || !d || d.channel !== CHANNEL || d.cmd !== cmd + '-reply') return;
        if (d.nonce !== nonce || !d.data) return;   // not ready yet — keep asking
        done(d.data);
      };
      const done = (data) => {
        window.removeEventListener('message', onMessage);
        clearInterval(timer);
        resolve(data);
      };

      window.addEventListener('message', onMessage);
      const ask = () => {
        if (Date.now() > deadline) return done(null);
        window.postMessage(Object.assign({ channel: CHANNEL, cmd, nonce }, extra), '*');
      };
      timer = setInterval(ask, 500);
      ask();
    });
  }

  /** The episode's title/season/number, for pairs that have to be matched by name. */
  const pageMeta = (timeoutMs = 15000) => askPage('meta', {}, timeoutMs);

  /**
   * German audio, chosen for you.
   *
   * Everything this extension exists for is German-dubbed, and Netflix's own memory of that
   * choice is per profile and per title rather than absolute — a show it has never played in
   * German opens in its original language. So each episode is asked once, and it is a **read
   * before a write**: an episode already in German is left completely alone, which matters
   * more here than anywhere else in this file, because setting the track Netflix is already
   * playing still costs an audible re-buffer.
   *
   * Not part of the badge, deliberately. The badge is a claim about the subtitle hand-off, and
   * a track Netflix doesn't offer in German (there are plenty) is not a failure of anything —
   * it goes to the console, where the rest of this file's diagnosis lives.
   *
   * Once per episode, never watched: switching to Japanese halfway through is a decision, and
   * the next episode is where German starts again — the same rule the visibility pass follows.
   */
  async function germanAudio(myToken) {
    const done = await askPage('audio', { apply: true }, AUDIO_MS);
    if (myToken !== token || !done || done.state !== 'set') return done;

    // It answered "I set it"; Netflix's player is what says whether it took.
    await sleep(AUDIO_SETTLE_MS);
    if (myToken !== token) return done;
    const now = await askPage('audio', { apply: false }, AUDIO_MS);
    return Object.assign({ confirmed: Boolean(now && now.state === 'already') }, done);
  }

  /** The show's name as the player prints it — last resort if the page globals move again. */
  function domTitle() {
    const h4 = document.querySelector('[data-uia="video-title"] h4');
    return (h4 && h4.textContent.trim()) || null;
  }

  function badge(state) {
    console.debug('[gigaku] badge →', state);
    chrome.runtime.sendMessage({ type: 'badge', state }).catch(() => {});
  }

  // ── reading Migaku's UI ────────────────────────────────────────────────────

  function migakuRoot() {
    const host = document.getElementById('MigakuShadowDom');
    return (host && host.shadowRoot) || null;
  }

  /**
   * Migaku's toolbar. Everything this file feeds and reads hangs off it, and Migaku renders it
   * only when `isLangReady && isAppWindowOpen && isDomainEnabled` — so its absence is not a
   * timing accident to wait out, it is Migaku telling us its app window is closed.
   */
  function toolbar() {
    const root = migakuRoot();
    return (root && root.querySelector('.Toolbar')) || null;
  }

  /** The subtitle half of the video-options popover, or null while it isn't mounted. */
  function panel() {
    const root = migakuRoot();
    return (root && root.querySelector('.SubtitleSettings')) || null;
  }

  const fileInput = (box) => box.querySelector('input[type="file"][accept*="srt" i]');

  /**
   * The two slots — target first, secondary second — each one control container holding a file
   * input and, when Migaku feels like rendering it, a dropdown. They are read per container
   * rather than by global order, so Migaku growing a third file input somewhere in the panel
   * can't shuffle the pair, and it is the container element that gets handed around: Migaku
   * swaps its children as it re-renders, the div itself stays.
   *
   * A slot needs only its INPUT, and that is the point. Read off the bundle, a container
   * renders its input unconditionally and its dropdown only when
   * `(filtered options > 1) && targetSubtitleData !== undefined` — i.e. only once Migaku
   * already holds a usable track for the episode. Requiring the dropdown before feeding
   * therefore refused the good path in exactly the case this extension exists for: an episode
   * with no subtitles in the language being learned — which is *why* its track was ASR-ripped —
   * where Migaku shows "none available" and no amount of waiting can produce what we waited
   * for. The dropdown is the receipt, and feeding is what earns it: registering our file is
   * what puts a second option in that list and makes the dropdown render at all.
   */
  function slots() {
    const p = panel();
    if (!p) return null;
    const boxes = Array.from(p.querySelectorAll('.SubtitleSettings__controlContainer'))
      .filter(fileInput);
    return boxes.length >= 2 ? boxes.slice(0, 2) : null;
  }

  /** What that slot's dropdown names — '' when there is no dropdown, or nothing selected. */
  function label(box) {
    const single = box.querySelector('.multiselect__single');
    return ((single && single.textContent) || '').trim();
  }

  /**
   * Why there weren't two slots to feed — the one question a `sent` badge leaves open, and one
   * the panel can answer itself: no panel at all, containers with no input, or (the ordinary
   * case) containers sitting on Migaku's spinner or its "none available".
   */
  function panelState() {
    const p = panel();
    if (!p) return { panel: false, toolbar: Boolean(toolbar()) };
    return {
      panel: true,
      boxes: Array.from(p.querySelectorAll('.SubtitleSettings__controlContainer')).map((box) => ({
        input: Boolean(fileInput(box)),
        dropdown: Boolean(box.querySelector('.multiselect')),
        loading: Boolean(box.querySelector('.SubtitleSettings__loadingSubs')),
        none: Boolean(box.querySelector('.SubtitleSettings__noneAvailable')),
      })),
    };
  }

  /** Migaku's toolbar button for the video-options popover, found by its icon. */
  function ccTrigger() {
    const root = migakuRoot();
    if (!root) return null;
    const icon = Array.from(root.querySelectorAll('svg path'))
      .find((p) => (p.getAttribute('d') || '').startsWith(CC_ICON));
    if (!icon) return null;
    return icon.closest('.UiPopover__reference__element')
      || icon.closest('button, [role="button"]')
      || null;
  }

  /**
   * Click that opens/closes the popover. It must NOT bubble: the popover's own
   * click-away listener sits on `document` and closes on any click that isn't accompanied by
   * the mouse being over it — a bubbling synthetic click opens and instantly re-closes it.
   */
  function poke(el) {
    el.dispatchEvent(new MouseEvent('click', { bubbles: false, cancelable: true }));
  }

  function waitFor(check, timeoutMs, myToken) {
    return new Promise((resolve) => {
      const deadline = Date.now() + timeoutMs;
      const tick = () => {
        if (myToken !== undefined && myToken !== token) return resolve(null);
        const value = check();
        if (value) return resolve(value);
        if (Date.now() > deadline) return resolve(null);
        setTimeout(tick, 100);
      };
      tick();
    });
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ── handing files to Migaku ────────────────────────────────────────────────

  function srtFile(name, text) {
    return new File([text], name, { type: 'text/plain' });
  }

  function langHinted(name, lang) {
    return name.replace(/\.srt$/i, '') + '.' + lang + '.srt';
  }

  /**
   * Put one file in one slot and wait for Migaku's dropdown to name it. The label is allowed
   * to merely *change* as well as to contain the filename: Migaku labels a local track by its
   * own `languageDescription`, and a slot that was showing something else and now shows
   * something else again has still been re-pointed by our file — nothing else touched it.
   *
   * A slot with no dropdown yet reads as '' and satisfies neither test, so waiting here is
   * also how the one that renders *because* of this feed is waited for.
   */
  async function feedSlot(box, file, myToken) {
    const input = fileInput(box);
    if (!input) return false;
    const before = label(box);
    const stem = file.name.replace(/\.srt$/i, '');

    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));

    // Migaku clears the input as it reads the FileList; no reset means its handler never ran.
    if (!(await waitFor(() => input.value === '', CONSUMED_MS, myToken))) return false;

    return Boolean(await waitFor(() => {
      const now = label(box);
      return Boolean(now) && (now.includes(stem) || now !== before);
    }, SELECTED_MS, myToken));
  }

  // ── making both tracks visible ─────────────────────────────────────────────
  //
  // A track in its slot is not a track on screen, and the gap between the two is where this
  // extension used to go quiet. Migaku hides a loaded track through three settings, all
  // persisted per learning language — so one keypress ("w", "shift+w") or one study preset
  // outlives by weeks the episode it was set in, and the next episode opens with a green tick
  // over subtitles nobody can see:
  //
  //   1. `hideTargetSubtitles` / `hideSecondarySubtitles` — the two "Always hide …" switches
  //      in the very panel we feed through.
  //   2. `playMode.type`. Read off `PLAY_MODE_PRESETS` (file.methods-*.js), five of the eight
  //      presets obscure by design: **primed listening hides both** (secondary until you
  //      pause), intensive reading / intensive listening / show-on-pause / one-T hide until
  //      their reveal condition fires. Only default and intensive hybrid show both outright.
  //   3. `playMode.custom.obscuring.{target,secondary}.effect` — none/blur/hidden. This is the
  //      one the preset row cannot answer, because it applies exactly when the preset *is*
  //      custom, and it is why the row is only consulted when the custom panel is absent.
  //
  // Every one of them is read before it is written, so a Migaku already set the way we want it
  // is never clicked — this runs on every episode and is a no-op on almost all of them. And
  // every write is confirmed by a class Migaku binds to its own store (`-toggled`, `--active`,
  // `aria-checked`), never by the change we just made to the control: the same distinction the
  // feed makes between Migaku reading a file and Migaku loading it.
  //
  // Two things it deliberately does NOT do. It does not touch a language it never fed a file
  // to — see SHOW_FOR_LANG; these settings are per learning language, and Japanese runs Primed
  // Listening, a mode whose whole point is hiding both tracks. And it does not fight for it
  // mid-episode: a `w` pressed while watching is a decision about this minute, and the next
  // episode is where "show me both tracks" starts again — so this runs once per episode, with
  // the feed, and never watches.

  /** The two "Always hide …" switches, target first. */
  function hideSwitches() {
    const p = panel();
    const box = p && p.querySelector('.SubtitleSettings__hideSubtitles');
    return box ? Array.from(box.querySelectorAll('.UiToggle')) : [];
  }

  /**
   * Un-hide one of them. The checkbox is set rather than clicked: a click bubbles to the
   * document, where the popover's click-away listener is waiting to close the panel out from
   * under the rest of this pass — the same trap `poke` exists for. Migaku's handler reads
   * `e.target.checked` off the change event, so setting the property is all it wants.
   */
  async function unhide(i, myToken) {
    const at = () => hideSwitches()[i] || null;
    const hidden = () => {
      const toggle = at();
      return Boolean(toggle && toggle.classList.contains('-toggled'));
    };
    if (!at()) return false;
    if (!hidden()) return true;                    // already showing — nothing to click

    const input = at().querySelector('input.UiToggle__input');
    if (!input) return false;
    input.checked = false;
    input.dispatchEvent(new Event('change', { bubbles: true }));

    return Boolean(await waitFor(() => !hidden(), SETTING_MS, myToken));
  }

  const modeItems = () => {
    const root = migakuRoot();
    return root ? Array.from(root.querySelectorAll('.PlayMode__presets__item')) : [];
  };

  const isDefaultMode = (el) => Array.from(el.querySelectorAll('svg path'))
    .some((p) => (p.getAttribute('d') || '').startsWith(DEFAULT_MODE_ICON));

  /** The custom mode's per-track effect rows — three buttons each, "off" first. Present only
   *  while the custom mode is the active one, which is what makes them the discriminator. */
  const obscuringRows = () => {
    const root = migakuRoot();
    if (!root) return [];
    return Array.from(root.querySelectorAll('.SubtitleObscuring__buttons'))
      .map((box) => Array.from(box.querySelectorAll('[role="radio"]')))
      .filter((buttons) => buttons.length === 3);
  };

  /** Put one custom effect row back to "off". */
  async function showRow(i, myToken) {
    const at = () => obscuringRows()[i] || null;
    const off = () => {
      const row = at();
      return Boolean(row && row[0].getAttribute('aria-checked') === 'true');
    };
    const row = at();
    if (!row) return false;
    if (off()) return true;
    poke(row[0]);
    return Boolean(await waitFor(off, SETTING_MS, myToken));
  }

  /**
   * Take the obscuring off whichever of the two play-mode surfaces is showing. The custom
   * panel wins when it is there: it is only rendered while the custom preset is active, and in
   * that state the preset row has nothing left to say.
   */
  async function unobscure(myToken) {
    const rows = obscuringRows();
    if (rows.length) {
      const shown = [];
      for (let i = 0; i < rows.length; i += 1) shown.push(await showRow(i, myToken));
      return { custom: rows.length, shown };
    }

    const items = modeItems();
    if (items.length !== MODE_COUNT) return { modes: items.length };
    const active = items.findIndex((el) => el.classList.contains('--active'));
    if (!OBSCURING_MODES.has(active)) return { mode: active };
    if (!isDefaultMode(items[DEFAULT_MODE])) return { mode: active, defaultIcon: false };

    poke(items[DEFAULT_MODE]);
    const landed = await waitFor(() => {
      const now = modeItems()[DEFAULT_MODE];
      return Boolean(now && now.classList.contains('--active'));
    }, SETTING_MS, myToken);
    return { mode: active, toDefault: Boolean(landed) };
  }

  /** Which language Migaku is set to, straight off the attribute it publishes for it. */
  function selectedLang() {
    const root = migakuRoot();
    const app = root && root.querySelector('[' + LANG_ATTR + ']');
    return app ? app.getAttribute(LANG_ATTR) : null;
  }

  /**
   * Both passes, in the order that leaves nothing behind: switches first, then the mode — and
   * neither unless Migaku is on the language this extension actually feeds.
   */
  async function makeVisible(myToken) {
    const lang = selectedLang();
    if (lang !== SHOW_FOR_LANG) return { lang, skipped: true };

    const found = hideSwitches().length;
    const unhidden = [];
    for (let i = 0; i < Math.min(found, 2); i += 1) unhidden.push(await unhide(i, myToken));
    return { lang, switches: found, unhidden, mode: await unobscure(myToken) };
  }

  /**
   * What the viewer actually gets. Migaku puts `-hidden` on each track's list the moment
   * anything — a switch, a play mode, an empty track — takes it off screen, so this is the one
   * reading that covers all of it at once, and it needs no panel open to be read.
   *
   * A list that isn't there reads `null`, and unreadable is not the same as hidden: only a
   * Migaku that says `-hidden` out loud is allowed to hold the badge back.
   */
  function onScreen() {
    const root = migakuRoot();
    const read = (which) => {
      const list = root && root.querySelector('.SubtitleOverlay__' + which);
      return list ? !list.classList.contains('-hidden') : null;
    };
    return { target: read('targetSubs'), secondary: read('secondarySubs') };
  }

  /** Wait for it to settle — the settings we just wrote take a render or two to reach here. */
  async function onScreenSettles(myToken) {
    const settled = await waitFor(() => {
      const now = onScreen();
      return now.target !== false && now.secondary !== false ? now : null;
    }, SCREEN_MS, myToken);
    return { ok: Boolean(settled), reading: settled || onScreen() };
  }

  /** Migaku's own synthetic-drop path — slots are chosen by the filename's language. */
  function dropFiles(pair) {
    const files = [
      srtFile(langHinted(pair.primary.name, LANG_HINT.primary), pair.primary.text),
      srtFile(langHinted(pair.secondary.name, LANG_HINT.secondary), pair.secondary.text),
    ];
    document.dispatchEvent(new CustomEvent('drop', { detail: { types: ['Files'], files } }));
  }

  /**
   * One full attempt. Returns a badge state: 'ok' | 'sent' | 'hid' | 'err' | 'off' | 'app'.
   *
   * Every step it takes is recorded and, on anything short of success, printed — the whole
   * point of this file is that Migaku's UI is read rather than assumed, so when a Migaku
   * redesign moves what's read, the console should say which of the four hooks moved
   * (shadow root → popover trigger → panel → the two dropdowns) instead of just going red.
   */
  async function attempt(pair, myToken) {
    const trace = {};

    // Migaku's UI has to be on the page at all — without it there is nothing to feed, and
    // claiming success would light the badge green over subtitles nobody loaded.
    trace.migakuRoot = Boolean(await waitFor(migakuRoot, READY_MS, myToken));
    if (!trace.migakuRoot) return report('off', trace);

    // No toolbar means Migaku's app window is closed, so the worker opens it — unfocused, and
    // only if one isn't open already — and we wait for Migaku to come up.
    if (!toolbar()) {
      trace.appWindow = await ask({ type: 'appwindow' });
      if (myToken !== token) return 'err';
      trace.toolbar = Boolean(await waitFor(toolbar, TOOLBAR_MS, myToken));
      if (!trace.toolbar) return report('app', trace);
    }

    let opened = false;
    trace.panelWasOpen = Boolean(panel());
    if (!trace.panelWasOpen) {
      const trigger = ccTrigger();
      trace.trigger = Boolean(trigger);
      if (trigger) {
        poke(trigger);
        opened = Boolean(await waitFor(panel, PANEL_MS, myToken));
        trace.panelOpened = opened;
      }
    }

    try {
      const ready = await waitFor(slots, TRACKS_MS, myToken);
      if (myToken !== token) return 'err';
      trace.slots = Boolean(ready);
      if (!ready) {
        // No file inputs to feed — the drop path is the only thing left, and it reports
        // nothing back, so this can never be called loaded. Say what the panel was showing
        // instead: it is the whole of why we got here. The hide settings are still worth
        // putting right — if the drop does land, it lands on a Migaku set to show it.
        trace.showing = panelState();
        dropFiles(pair);
        trace.visible = await makeVisible(myToken);
        return report('sent', trace);
      }

      trace.primary = await feedSlot(ready[0], srtFile(pair.primary.name, pair.primary.text), myToken);
      // Feeding re-renders the panel (a new track in both dropdowns' options) — re-read it.
      const again = slots() || ready;
      trace.secondary = await feedSlot(again[1], srtFile(pair.secondary.name, pair.secondary.text), myToken);
      trace.labels = (slots() || again).map(label);
      if (!(trace.primary && trace.secondary)) return report('err', trace);

      // Loaded is not shown, and the settings that decide it outlive the episode that set
      // them — so the last thing every feed does is put them back to showing both.
      trace.visible = await makeVisible(myToken);
      // …on another language those settings are none of our business, and neither is the
      // screen they produce: the feed is what the badge answers for, and it worked.
      if (trace.visible.skipped) return report('ok', trace);

      const shown = await onScreenSettles(myToken);
      trace.visible.onScreen = shown.reading;
      return report(shown.ok ? 'ok' : 'hid', trace);
    } finally {
      if (opened && panel()) {
        const trigger = ccTrigger();
        if (trigger) poke(trigger);
      }
    }
  }

  /**
   * The trace is printed as TEXT, not as an object. Where these are actually read —
   * chrome://extensions' Errors page — every console argument is flattened with String(), so an
   * object arrives as `[object Object]`: the one line whose job is to name the hook that moved
   * says nothing at all, which is how a `sent` badge stayed unexplained.
   */
  function report(state, trace) {
    if (state !== 'ok') {
      console.warn('[gigaku] hand-off to Migaku ended as ' + state + ' — ' + JSON.stringify(trace));
    }
    return state;
  }

  // ── the loop ───────────────────────────────────────────────────────────────

  /**
   * A round trip to the service worker, which is never allowed to be the thing that hangs:
   * `sendMessage` waits forever on a listener that answers neither way, and the badge would
   * sit on '…' with nothing to show for it. A silent worker is the same as no answer.
   */
  async function ask(message) {
    try {
      return await Promise.race([
        chrome.runtime.sendMessage(message),
        new Promise((r) => setTimeout(() => r(null), TALK_MS)),
      ]);
    } catch (e) {
      return null;   // service worker restarting / extension reloaded — the next tick retries
    }
  }

  async function load(id, myToken) {
    badge('wait');

    // Netflix's audio track has nothing to do with Migaku, the library or the badge, so it is
    // started here and not waited for: a missing subtitle pair, or a Migaku that never comes
    // up, must not cost the episode its German soundtrack.
    germanAudio(myToken)
      .then((r) => console.debug('[gigaku] audio →', JSON.stringify(r)))
      .catch((e) => console.warn('[gigaku] could not choose the German audio track:', e));

    // Migaku switched off in chrome://extensions can be told apart from Migaku merely being
    // slow, and instantly — no point spending a minute waiting for a UI that cannot come.
    const migaku = await ask({ type: 'migaku' });
    console.debug('[gigaku] Migaku is', migaku ? migaku.state : 'unknown (worker silent)');
    if (myToken !== token) return;
    if (migaku && migaku.state !== 'enabled') return badge('off');

    // The id alone settles everything `gigaku subs` ripped (it records the Netflix id), so try
    // that first and don't wait on Netflix's player metadata unless it misses.
    let pair = await ask({ type: 'lookup', id });
    if (myToken !== token) return;
    if (!pair) {
      const meta = await pageMeta();
      if (myToken !== token) return;
      if (meta) {
        if (!meta.title) meta.title = domTitle();
        if (meta.title) pair = await ask({ type: 'lookup', id, meta });
      }
    }
    if (myToken !== token) return;
    if (!pair) return badge('none');

    let state = 'err';
    for (let i = 0; i < ATTEMPTS; i += 1) {
      try {
        state = await attempt(pair, myToken);
      } catch (e) {
        console.warn('[gigaku] could not hand subtitles to Migaku:', e);
        state = 'err';
      }
      if (myToken !== token) return;   // episode changed mid-feed — that run owns the badge now
      if (state === 'ok' || state === 'off' || state === 'app') break;
      if (i + 1 < ATTEMPTS) await sleep(BETWEEN_MS);
      if (myToken !== token) return;
    }
    badge(state);
  }

  function tick() {
    const id = watchId();
    if (id === currentId) return;
    currentId = id;
    token += 1;
    if (!id) return badge('clear');
    load(id, token);
  }

  // Clicking the toolbar icon asks for another go; the worker reloads the tab itself when we
  // don't answer, which is the case Migaku can't be fixed from inside the page anyway.
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== 'retry') return false;
    const inPage = Boolean(migakuRoot());
    if (inPage && currentId) {
      token += 1;
      load(currentId, token);
    }
    sendResponse({ migaku: inPage });
    return false;
  });

  setInterval(tick, POLL_MS);
  tick();
})();
