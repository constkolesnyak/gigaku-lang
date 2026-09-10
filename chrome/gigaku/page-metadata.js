/* MAIN-world half of the extension: the only things here that a content script can't do.
 *
 * Netflix keeps both of them on page globals (`netflix.appContext`), and content scripts run
 * in an isolated world that shares the DOM but not page globals — so this file is registered
 * with `"world": "MAIN"` and answers over window.postMessage. Two questions:
 *
 *   `meta`  — the episode's title/season/number. Only a *fallback*: an episode whose Netflix
 *             id is in subs/index.json (everything `gigaku subs` rips) is matched by id
 *             straight from the URL and never needs this. It covers files copied in by hand,
 *             which are named by title and have no id.
 *             Same source as lib/subs/subs.py::_season_episodes():
 *             videoMetadata._seasons[]._episodes[]._video.
 *
 *   `audio` — put the episode on its German soundtrack, through Netflix's own player API
 *             (`getAudioTrackList` / `getAudioTrack` / `setAudioTrack`) rather than its menu,
 *             so there is nothing to break when Netflix redesigns or changes language.
 *
 * Both reply `{cmd: '<cmd>-reply', nonce, data}`, and `data: null` means *not yet, ask again*
 * — the player boots well after this file runs, so an empty answer is the normal opening
 * state rather than a no.
 */
(function () {
  'use strict';

  const CHANNEL = 'gigaku-subs-channel';

  // The soundtrack this extension exists to watch things in. Regional variants count.
  const AUDIO_LANG = 'de';

  function currentId() {
    const m = location.pathname.match(/\/watch\/(\d+)/);
    return m ? Number(m[1]) : null;
  }

  function seriesTitle(vm) {
    // The show's title — measured on the live player, not guessed: `_metadataObject.video.title`
    // is where it sits (`_video.title` is undefined there). The others are cheap insurance
    // against Netflix moving it again; `content.js` falls back to the on-screen title if all
    // of them come up empty.
    try {
      const meta = vm._metadataObject;
      if (meta && meta.video && meta.video.title) return meta.video.title;
    } catch (e) { /* fall through */ }
    return (vm && vm._video && vm._video.title) || (vm && vm.title) || null;
  }

  /** {title, season, episode} for the episode in the URL, or null if it can't be read yet. */
  function readMeta() {
    const id = currentId();
    if (id === null) return null;
    let vm;
    try {
      const all = netflix.appContext.state.playerApp.getState().videoPlayer.videoMetadata;
      vm = all[Object.keys(all)[0]];
    } catch (e) {
      return null;
    }
    if (!vm) return null;
    const title = seriesTitle(vm);

    const seasons = vm._seasons || [];
    for (let i = 0; i < seasons.length; i++) {
      const episodes = seasons[i]._episodes || [];
      for (const ep of episodes) {
        const v = ep._video || {};
        if (v.episodeId === id) return { id, title, season: i + 1, episode: v.seq };
      }
    }
    // In no season → a movie: one file, no episode numbering (matches naming.srt_base).
    return { id, title, season: null, episode: 1 };
  }

  /**
   * The player for the episode in the URL — never `[0]`.
   *
   * Right after Netflix swaps episodes it keeps the previous one's session around and their
   * order isn't stable, so the first entry can be a stale player whose setters silently
   * no-op (the same trap lib/subs/subs.py::_PLAYER exists for). The movie id is the
   * discriminator.
   *
   * A closed session is only *deprioritised*, not skipped, and that is measured: on a live,
   * playing episode `isVideoPlayerClosedForSessionId` answered **true** for the one and only
   * session, whose `getMovieId()` matched the URL exactly. Requiring an open one there finds
   * no player at all, so it breaks the tie rather than settling the question.
   */
  function player() {
    let api;
    try {
      api = netflix.appContext.state.playerApp.getAPI().videoPlayer;
    } catch (e) {
      return null;
    }
    const want = currentId();
    if (want === null) return null;

    let closedMatch = null;
    for (const id of api.getAllPlayerSessionIds() || []) {
      try {
        const p = api.getVideoPlayerBySessionId(id);
        if (!p || p.getMovieId() !== want) continue;
        if (!api.isVideoPlayerClosedForSessionId || !api.isVideoPlayerClosedForSessionId(id)) return p;
        closedMatch = closedMatch || p;
      } catch (e) { /* a session that won't answer isn't the one */ }
    }
    return closedMatch;
  }

  const isGerman = (track) => Boolean(track) && typeof track.bcp47 === 'string'
    && (track.bcp47 === AUDIO_LANG || track.bcp47.startsWith(AUDIO_LANG + '-'));

  /**
   * Read which soundtrack is playing and, with `apply`, put it on German.
   *
   * Read first, always: re-selecting the track Netflix is already playing still costs an
   * audible re-buffer, so "already German" has to be a state this can report rather than
   * something it re-asserts every episode. `null` means the player isn't up yet — ask again.
   *
   * PRIMARY is preferred over Netflix's other track types so a German *audio description*
   * track can never win over the plain German dub.
   */
  function audio(apply) {
    const p = player();
    if (!p) return null;

    let list;
    let current;
    try {
      list = p.getAudioTrackList() || [];
      current = p.getAudioTrack();
    } catch (e) {
      return null;
    }
    if (!list.length) return null;
    if (isGerman(current)) return { state: 'already', track: current.displayName };

    const german = list.filter(isGerman);
    const want = german.filter((t) => t.trackType === 'PRIMARY')[0] || german[0];
    const was = (current && current.displayName) || null;
    if (!want) return { state: 'missing', was, offered: list.map((t) => t.bcp47) };
    if (!apply) return { state: 'wrong', was };

    try {
      p.setAudioTrack(want);
    } catch (e) {
      return { state: 'failed', was, why: String((e && e.message) || e) };
    }
    return { state: 'set', was, track: want.displayName };
  }

  const ANSWERS = {
    meta: () => readMeta(),
    audio: (msg) => audio(Boolean(msg.apply)),
  };

  window.addEventListener('message', (ev) => {
    if (ev.source !== window) return;
    const msg = ev.data;
    if (!msg || msg.channel !== CHANNEL) return;
    const answer = ANSWERS[msg.cmd];
    if (!answer) return;

    let data = null;
    try {
      data = answer(msg);
    } catch (e) {
      data = null;                            // not ready, or Netflix moved something
    }
    window.postMessage({ channel: CHANNEL, cmd: msg.cmd + '-reply', nonce: msg.nonce, data }, '*');
  });
})();
