/* Service worker: resolve the episode playing in a tab to an SRT pair inside subs/, read the
 * files, and keep the toolbar badge in sync.
 *
 * The library lives inside the extension folder, so no server, no file:// permission and no
 * folder picker are involved — a subtitle is just an extension resource. Only the worker
 * fetches them (an extension-internal fetch from here needs no web_accessible_resources) and
 * it passes the text to the content script, which is what actually talks to Migaku.
 *
 * Matching, in order:
 *   1. the Netflix video id from the URL, via subs/index.json (`gigaku subs` records the id of
 *      every episode it rips — exact, and immune to title/locale differences);
 *   2. title + season + episode from the same index (files written by `gigaku srt`);
 *   3. the deterministic path `subs/<Title>/<Title> - S01E03 - Primary.srt`, so files copied
 *      in by hand work with no index at all.
 */
'use strict';

/* The badge is a claim about Migaku's state, so every state here is one this extension can
 * actually establish — `ok` means both of Migaku's slots were read back and name our files,
 * not merely that they were handed over. `sent` is the honest middle: the files went to
 * Migaku's drag-and-drop entry point, which answers nothing.
 *
 * `ok` also means both tracks are *on screen*, which is a second claim and a separate one:
 * a track can sit loaded in its slot and still be hidden by a Migaku setting that outlives
 * the episode it was set in. `hid` is that case — the files are there, the switch isn't. */
const BADGES = {
  wait:  { text: '…', color: '#6b7280', title: 'Looking for subtitles…' },
  ok:    { text: '✓', color: '#2e7d32', title: 'Both subtitle tracks are loaded and shown in Migaku' },
  none:  { text: '–', color: '#6b7280', title: 'No subtitles for this episode' },
  sent:  { text: '?', color: '#f9a825', title: 'Subtitles handed to Migaku, but it never confirmed them — click to retry' },
  hid:   { text: 'H', color: '#f9a825', title: 'Subtitles are loaded, but Migaku is still not showing a track — click to retry' },
  err:   { text: '!', color: '#c62828', title: 'Migaku would not take the subtitles — click to retry' },
  off:   { text: 'M', color: '#f9a825', title: 'Migaku is not running on this page — click to fix' },
  app:   { text: 'A', color: '#f9a825', title: "Migaku's app window was opened but Migaku still isn't up on this page — click to retry" },
  clear: { text: '',  color: '#6b7280', title: 'Gigaku' },
};

// Migaku (early access). Only ever read, and — on a click of our own toolbar icon — enabled.
const MIGAKU_ID = 'dmeppfcidcpcocleneopiblmpnbokhep';

async function migakuState() {
  try {
    const info = await chrome.management.get(MIGAKU_ID);
    return info.enabled ? 'enabled' : 'disabled';
  } catch (e) {
    return 'missing';                             // not installed, or a different build id
  }
}

/** Why Migaku isn't in the page — the three cases need three different fixes from the user. */
const OFF_TITLES = {
  disabled: 'Migaku is switched off in Chrome — click to switch it on and reload',
  missing:  'Migaku is not installed',
  enabled:  'Migaku did not load on this page — click to reload',
};

/* Migaku's app window, and why this extension opens it.
 *
 * Migaku renders its toolbar only when `isParsingReady && !isMigakuMemory && isDomainEnabled`,
 * and `isParsingReady` is `isLangReady && isAppWindowOpen`. With that window closed there is no
 * toolbar, therefore no Target/Secondary dropdowns and no file inputs — nothing to feed and,
 * worse, nothing to read back. That is the state the old green tick was lying about.
 *
 * The flag is set by the app window's own page (`created(){ this.isAppWindowOpen = true }`),
 * so opening that page is all it takes, and Migaku publishes it as a web-accessible resource.
 * It is opened UNFOCUSED, so a rip stays out of the way. Never open a second one: Migaku's app
 * window watches for a duplicate and answers by setting `isAppWindowOpen` back to false, which
 * would leave things worse than not trying. */
const APP_WINDOW = `chrome-extension://${MIGAKU_ID}/pages/app-window/index.html`;
const APP_WINDOW_GLOB = `chrome-extension://${MIGAKU_ID}/pages/app-window/*`;
const FOCUS_GUARD_MS = 8000;

/* A window of its own, unfocused. Migaku's app page in a background *tab* costs no animation
 * and no focus, but Chrome throttles timers in a hidden tab and Migaku does not survive that
 * — measured, don't retry it. So the window stays, and with it macOS's animation. */
async function ensureAppWindow(homeWindowId) {
  try {
    const open = await chrome.tabs.query({ url: APP_WINDOW_GLOB });
    if (open.length) return { state: 'open' };
    const guard = handBack(homeWindowId);
    const win = await chrome.windows.create({
      url: APP_WINDOW, type: 'popup', focused: false, width: 460, height: 640, top: 40, left: 40,
    });
    guard.watch(win && win.id);
    return { state: 'opened' };
  } catch (e) {
    return { state: 'refused', why: String((e && e.message) || e) };
  }
}

/* Migaku pulls focus to its app window itself the moment it comes up — its own
 * `requestShowAppWindow` defaults to `focusWindow: true` and fires on `isAppWindowOpen`
 * turning true — so no argument to `windows.create` prevents it, and the episode would lose
 * focus to a window nobody asked to look at. Focus is therefore not fought for but handed
 * back: for a few seconds, any time that one window takes focus, the window the episode is
 * playing in gets it straight back. Only that window is countered and only briefly, so
 * deliberately clicking into Migaku still works — it just has to be your click.
 */
function handBack(homeWindowId) {
  if (homeWindowId === undefined) return { watch: () => {} };
  let appWindowId = null;

  const onFocus = (id) => {
    if (appWindowId !== null && id === appWindowId) {
      chrome.windows.update(homeWindowId, { focused: true }).catch(() => {});
    }
  };
  chrome.windows.onFocusChanged.addListener(onFocus);
  setTimeout(() => chrome.windows.onFocusChanged.removeListener(onFocus), FOCUS_GUARD_MS);

  return {
    watch: (id) => {
      appWindowId = id === undefined ? null : id;
      chrome.windows.update(homeWindowId, { focused: true }).catch(() => {});
    },
  };
}

// Mirrors lib/subs/naming.py::sanitize — the two must agree or a season splits in half.
const ILLEGAL = /[<>:"/\\|?*\x00-\x1f]+/g;

function sanitize(title) {
  return String(title).replace(ILLEGAL, ' ').trim() || 'Subtitles';
}

function srtBase(title, season, episode) {
  const name = sanitize(title);
  if (!season) return name;                       // a movie: one export, no numbering
  return `${name} - S${String(season).padStart(2, '0')}E${String(episode).padStart(2, '0')}`;
}

function titleKey(title, season, episode) {
  return `${sanitize(title).toLowerCase()}|${season || 0}|${episode}`;
}

/** Extension URL for a library-relative path, cache-busted so a just-ripped file isn't missed. */
function resourceUrl(path) {
  const encoded = path.split('/').map(encodeURIComponent).join('/');
  return chrome.runtime.getURL(`subs/${encoded}`) + `?v=${Date.now()}`;
}

async function readText(path) {
  const res = await fetch(resourceUrl(path));
  return res.ok ? res.text() : null;
}

async function readIndex() {
  try {
    const res = await fetch(resourceUrl('index.json'));
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;                                  // no index yet — path guessing still works
  }
}

/** {dir, primary, secondary} for this episode, or null. */
async function resolveEntry(id, meta) {
  const index = await readIndex();
  if (index) {
    const byTitle = index.byTitle || {};
    const keyById = (index.byId || {})[String(id)];
    if (keyById && byTitle[keyById]) return byTitle[keyById];
    if (meta) {
      const entry = byTitle[titleKey(meta.title, meta.season, meta.episode)];
      if (entry) return entry;
    }
  }
  if (!meta) return null;
  const dir = sanitize(meta.title);
  const base = srtBase(meta.title, meta.season, meta.episode);
  return { dir, primary: `${base} - Primary.srt`, secondary: `${base} - Secondary.srt` };
}

async function lookup(id, meta) {
  const entry = await resolveEntry(id, meta);
  if (!entry) return null;
  const [primary, secondary] = await Promise.all([
    readText(`${entry.dir}/${entry.primary}`),
    readText(`${entry.dir}/${entry.secondary}`),
  ]);
  if (!primary || !secondary) return null;        // a lone Primary is not a usable pair
  return {
    primary: { name: entry.primary, text: primary },
    secondary: { name: entry.secondary, text: secondary },
  };
}

/** `title` overrides the state's own wording, for the cases that have something to add. */
async function setBadge(tabId, state, title) {
  const badge = BADGES[state] || BADGES.clear;
  chrome.action.setBadgeText({ tabId, text: badge.text });
  chrome.action.setBadgeBackgroundColor({ tabId, color: badge.color });
  if (title) return chrome.action.setTitle({ tabId, title });
  const stated = state === 'off' ? OFF_TITLES[await migakuState()] : badge.title;
  chrome.action.setTitle({ tabId, title: stated || badge.title });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ENABLE_MS = 20000;                          // how long Chrome's own prompt may take

/* Clicking the icon means "fix it". Chrome will not let an extension enable another one
 * without a user gesture (and it still asks the user itself), which is exactly what this
 * click is — so the one thing the page cannot do for itself is the one thing a click does.
 * A content script is only injected at load time, so switching Migaku on is always followed
 * by a reload; and a Migaku that is on but absent from the page is that same case.
 *
 * `setEnabled` is called FIRST, synchronously, before anything is awaited: a service worker
 * holds the click's user gesture only until the first `await`, and spending it on so much as
 * reading Migaku's state leaves `setEnabled` to be ignored — it then neither resolves nor
 * rejects, which is how a click used to leave the badge stuck on "…" forever. Asking to
 * enable an already-enabled extension is a no-op, so it is free to ask before deciding. */
chrome.action.onClicked.addListener((tab) => {
  if (!tab || tab.id === undefined) return;
  const enabling = chrome.management.setEnabled(MIGAKU_ID, true).then(
    () => ({ ok: true }),
    (e) => ({ ok: false, why: String((e && e.message) || e) }),
  );
  fix(tab.id, enabling);
});

async function fix(tabId, enabling) {
  setBadge(tabId, 'wait');
  // Chrome's confirmation is a dialog we neither see nor control, so the promise is a hint,
  // not a gate: whatever it does, Migaku's own state is what decides, a prompt nobody answers
  // ends as a badge rather than as silence, and whatever Chrome said ends up in the tooltip —
  // an extension that can't say why it failed is the thing this whole file exists to avoid.
  const answer = await Promise.race([enabling, sleep(ENABLE_MS).then(() => ({ ok: false, why: 'Chrome never answered' }))]);
  const state = await migakuState();
  console.log('[gigaku] asked Chrome to switch Migaku on:', answer, '→ Migaku is', state);

  if (state !== 'enabled') {
    // Chrome kept it off. Say so, and put the toggle one click away instead of leaving the
    // user to find it.
    setBadge(tabId, 'off', `Chrome would not switch Migaku on (${answer.why || 'refused'}) — do it on the page that just opened, then reload the episode`);
    chrome.tabs.create({ url: `chrome://extensions/?id=${MIGAKU_ID}` });
    return;
  }

  const reply = await chrome.tabs.sendMessage(tabId, { type: 'retry' }).catch(() => null);
  if (!reply || !reply.migaku) chrome.tabs.reload(tabId);
}

// The right mouse button's fullscreen (see remote-keys.js), and it toggles the **window**
// rather than an element inside the page. Element fullscreen was measured doing two wrong
// things at once: leaving it did not make Chrome small again — it never touched the window, so
// "small screen" left a full-size Chrome behind — and entering it made Netflix resume a paused
// video off its own `fullscreenchange` (traced three times running: `was: paused → now:
// playing`). `chrome.windows.update` needs no user gesture, and the page never learns it
// happened, so neither failure has anywhere to come from.
async function toggleWindow(windowId) {
  if (windowId === undefined) return null;
  const win = await chrome.windows.get(windowId);
  const state = win.state === 'fullscreen' ? 'normal' : 'fullscreen';
  await chrome.windows.update(windowId, { state });
  return state;
}

// A content script keeps running in pages that were already open when the extension reloads,
// but its `chrome.runtime` handle dies with the old extension context. Measured: every right
// button press came back `Error: Extension context invalidated.` while the left one kept
// working, because play/pause touches no extension API and the window toggle is the one thing
// that must ask the worker. Chrome does not re-inject on its own, so this does. `remote-keys.js`
// carries a generation marker, so the orphan it lands beside stands down instead of doubling
// every press — two live listeners would toggle twice and look like the button doing nothing.
async function reinjectRemoteKeys() {
  const tabs = await chrome.tabs.query({ url: '*://*.netflix.com/*' });
  await Promise.all(
    tabs.map((t) =>
      chrome.scripting
        .executeScript({ target: { tabId: t.id }, files: ['remote-keys.js'] })
        .catch(() => {}),                        // a tab mid-navigation is not an error
    ),
  );
}

chrome.runtime.onInstalled.addListener(reinjectRemoteKeys);
chrome.runtime.onStartup.addListener(reinjectRemoteKeys);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const tabId = sender.tab && sender.tab.id;
  if (msg && msg.type === 'windowstate') {
    toggleWindow(sender.tab && sender.tab.windowId)
      .then((state) => sendResponse({ state }))
      .catch((e) => {
        console.warn('[gigaku] window state failed:', e);
        sendResponse(null);
      });
    return true;                                  // async response
  }
  if (msg && msg.type === 'badge') {
    if (tabId !== undefined) setBadge(tabId, msg.state);
    sendResponse(true);
    return false;
  }
  if (msg && msg.type === 'migaku') {
    migakuState().then((state) => sendResponse({ state }));
    return true;                                  // async response
  }
  if (msg && msg.type === 'appwindow') {
    // The episode's own window is what focus is handed back to.
    ensureAppWindow(sender.tab && sender.tab.windowId).then(sendResponse);
    return true;                                  // async response
  }
  if (msg && msg.type === 'lookup') {
    lookup(msg.id, msg.meta)
      .then(sendResponse)
      .catch((e) => {
        console.warn('[gigaku] lookup failed:', e);
        sendResponse(null);
      });
    return true;                                  // async response
  }
  return false;
});
