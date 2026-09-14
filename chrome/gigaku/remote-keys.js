// The mouse's two buttons, executed here.
//
// `scripts/scroll_volume.py` turns the physical mouse into a remote: the wheel drives the Apple TV,
// the left button is play/pause and the right one is fullscreen. The daemon cannot do either
// itself, and the two reasons are different:
//
//   * Fullscreen needs a real user gesture. `element.requestFullscreen()` called from outside
//     one is refused — measured on this Netflix tab, visible and focused, with
//     `navigator.userActivation.isActive` reading true: `TypeError: Permissions check failed`.
//     Called from *inside* a trusted keydown it succeeded 8 times out of 8. So the daemon's
//     job is to deliver a gesture, and this file's job is to be inside it.
//   * Netflix's own `f` shortcut is not usable as that lever. Something on the watch page eats
//     it: all eight presses arrived correctly (`key:"f"`, `code:"KeyF"`, `isTrusted:true`) and
//     every one carried `defaultPrevented: true`, with zero `fullscreenchange` events. So this
//     listener sits in the **capture** phase on `window`, which runs before any handler on the
//     document, and consumes its keys outright.
//
// The keys are F17 and F18, and which function keys are usable was measured, not chosen. They
// exist in the HID tables but not on this keyboard, so they can never collide with something
// the user typed; they carry no character, so no keyboard layout can distort them (the first
// version sent `kVK_ANSI_F`, which the Russian layout delivered as "а" — that is why the right
// button never worked at all); and they are inert in every other app, so a press with Chrome in
// the background does nothing rather than typing into whatever the user is working in.
//
// macOS takes some of that range above the browser, and a key it takes never reaches the page
// while still firing whatever the system has bound to it. Measured with the tab focused and
// visible, one key at a time: F16–F20 all arrived; **F14 never did** (the system keeps F14/F15
// for screen brightness); and **F13 arrived once and then stopped**, which is what was reported
// as both buttons "changing the screen" — a system action firing while Chrome saw nothing.

const PLAY_PAUSE = "F17";
const FULLSCREEN = "F18";

// This file is also injected by hand after an extension reload (`reinjectRemoteKeys` in
// background.js), because the copy Chrome left in an already-open page keeps its listener but
// loses `chrome.runtime` with the old extension context. Two live listeners would toggle every
// press twice, which reads as the button doing nothing — so each instance claims a generation
// on the shared DOM and the older one stands down at its next press. A generation rather than a
// flag: the newcomer must win, and it cannot reach into the orphan's world to switch it off.
const GEN = String(Number(document.documentElement.dataset.gigakuRemoteGen || 0) + 1);
document.documentElement.dataset.gigakuRemoteGen = GEN;

// A content script's globals live in the extension's own isolated world, which nothing outside
// it can read — not the page, and not AppleScript's `execute javascript` (that gets a world of
// its own). So what this file did is written onto the DOM, the one thing every world shares:
// `data-gigaku-remote` says the listener is installed at all (the answer to "was the extension
// reloaded?", which is otherwise unanswerable from outside), and `data-gigaku-remote-log` keeps
// the last few actions with what the player looked like when they ran. Diagnosing this blind
// is what made the previous three attempts guesswork.
const LOG_KEEP = 12;

function trace(entry) {
  const el = document.documentElement;
  let log = [];
  try {
    log = JSON.parse(el.dataset.gigakuRemoteLog || "[]");
  } catch (_) {}
  log.push(entry);
  el.dataset.gigakuRemoteLog = JSON.stringify(log.slice(-LOG_KEEP));
}

function player() {
  return document.querySelector("video");
}

function togglePlay() {
  const v = player();
  if (!v) return;
  if (v.paused) v.play();
  else v.pause();
}

// The **window**, not an element on the page — see `toggleWindow` in background.js for the two
// measured failures that moved it there. An element fullscreen left over from an earlier build
// (or put there by Netflix's own `f`) would keep the page covering the screen whatever the
// window does, so it is cleared on the way past; that needs no user gesture, unlike entering it.
function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  chrome.runtime.sendMessage({ type: "windowstate" }, () => void chrome.runtime.lastError);
}

// Fullscreen must not change what the video is doing, and left alone it does: Netflix resumes a
// paused video when the screen changes under it. So the state is held for a moment afterwards
// rather than set once — a single correction would land before Netflix's. Any later press takes
// ownership (`token`), so this can never fight the user's own next click.
let holdToken = 0;
const HOLD_MS = 1500;

function holdPlayback(wasPaused, token) {
  const until = performance.now() + HOLD_MS;
  const tick = () => {
    if (token !== holdToken) return; // a newer press owns the player now
    const v = player();
    if (v && v.paused !== wasPaused) {
      if (wasPaused) v.pause();
      else v.play().catch(() => {});
    }
    if (performance.now() < until) setTimeout(tick, 120);
  };
  setTimeout(tick, 80);
}

window.addEventListener(
  "keydown",
  (e) => {
    if (e.key !== PLAY_PAUSE && e.key !== FULLSCREEN) return;
    // Checked before the event is consumed, not after: an orphan that swallowed the key and
    // then bowed out would be worse than one that never woke up.
    if (document.documentElement.dataset.gigakuRemoteGen !== GEN) return;
    // Nothing else may act on these. This is not tidiness: with the daemon still sending
    // Netflix's own `f`, the right button was resuming playback as a side effect of whatever
    // else on the page had claimed that key. Consuming the event outright is what makes the
    // button do one thing.
    e.preventDefault();
    e.stopImmediatePropagation();
    // Thrown work must not escape into the page's key handling, so each action is guarded
    // rather than trusted — a torn-down player (Netflix drops the <video> in a tab that has
    // been hidden for hours) is the ordinary case, not an exceptional one.
    const v = player();
    const wasPaused = v ? v.paused : null;
    const token = ++holdToken; // every press cancels the previous press's hold
    const entry = {
      act: e.key === FULLSCREEN ? "fullscreen" : "play/pause",
      t: Math.round(performance.now()),
      was: v ? (v.paused ? "paused" : "playing") : "no-video",
      fs: !!document.fullscreenElement,
    };
    try {
      if (e.key === FULLSCREEN) {
        toggleFullscreen();
        if (wasPaused !== null) holdPlayback(wasPaused, token);
      } else {
        togglePlay();
      }
    } catch (err) {
      entry.err = String(err);
      console.log("[gigaku] remote key " + e.key + " failed: " + String(err));
    }
    // Read the result back rather than assume it: `play()` is async and Netflix may undo
    // either action, and "the button did nothing" and "the button did it and something put it
    // back" are the two diagnoses this has to be able to tell apart.
    setTimeout(() => {
      const w = player();
      entry.now = w ? (w.paused ? "paused" : "playing") : "no-video";
      entry.fsNow = !!document.fullscreenElement;
      trace(entry);
    }, 400);
  },
  true, // capture: ahead of Netflix's and Migaku's own document-level handlers
);

document.documentElement.dataset.gigakuRemote = "1";

