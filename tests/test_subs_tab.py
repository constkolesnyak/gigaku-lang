"""What ends a `gigaku subs` run — and what merely looks like it does.

Every auto-stop in the flow funnels through ``_check_tab``, and it used to decide by reading
"…not found in Chrome" out of an AppleScript error message. That error means only "no tab's
URL contains netflix.com/watch", which is true for a tab Netflix bounced to /browse as well as
for one the user closed — so a run that nobody stopped ended with "Stopped (Ctrl-C or tab
closed)". These pin the three answers apart, and pin that the message tells the truth.

Importing lib.subs.subs costs pyobjc, unlike the pure-logic modules the rest of the suite
covers; the browser itself is monkeypatched away.
"""
import pytest

from lib.config import NETFLIX_MATCH
from lib.subs import subs


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """The confirm/recover loops are deliberately patient. Tests shouldn't be."""
    monkeypatch.setattr(subs.time, "sleep", lambda _s: None)


def test_tab_state_reports_what_chrome_answered(monkeypatch):
    cases = [
        ("watch\nhttps://www.netflix.com/watch/80052801",
         ("watch", "https://www.netflix.com/watch/80052801")),
        ("away\nhttps://www.netflix.com/browse", ("away", "https://www.netflix.com/browse")),
        ("gone\n", ("gone", "")),
        (None, ("unknown", "")),  # Chrome answered nothing at all — NOT the same as "gone"
        ("", ("unknown", "")),
    ]
    for raw, want in cases:
        monkeypatch.setattr(subs, "_as", lambda _src, _raw=raw: _raw)
        assert subs._tab_state() == want


def test_a_closed_tab_stops_the_run_and_names_the_reason(monkeypatch):
    monkeypatch.setattr(subs, "_tab_state", lambda: ("gone", ""))
    monkeypatch.setattr(subs, "_chrome_tabs_total", lambda: 30)  # Chrome is fine, tab isn't
    with pytest.raises(subs._Aborted) as caught:
        subs._check_tab()
    assert "closed" in caught.value.reason


def test_silence_from_chrome_never_ends_a_season(monkeypatch):
    """The E16 failure, 2026-08-14: the run stopped with "the Netflix tab was closed" while
    that tab sat there on the episode it had just navigated to. `applescript.run` returns None
    when Chrome answers nothing, and that None was being read as "there is no Netflix tab" —
    silence taken for an answer, the same mistake the -2700 error string once caused."""
    monkeypatch.setattr(subs, "_tab_state", lambda: ("unknown", ""))
    with pytest.raises(subs.ExportError):     # recoverable: reload and ask again
        subs._check_tab()


def test_a_chrome_that_cannot_count_its_tabs_has_not_answered_either(monkeypatch):
    """"No Netflix tab" from a browser that also reports no tabs at all is not a reading."""
    monkeypatch.setattr(subs, "_tab_state", lambda: ("gone", ""))
    for total in (None, 0):
        monkeypatch.setattr(subs, "_chrome_tabs_total", lambda _t=total: _t)
        with pytest.raises(subs.ExportError):
            subs._check_tab()


def test_the_tab_count_is_read_as_a_number(monkeypatch):
    for raw, want in [("12", 12), (" 3 ", 3), ("", None), (None, None), ("lots", None)]:
        monkeypatch.setattr(subs, "_as", lambda _src, _raw=raw: _raw)
        assert subs._chrome_tabs_total() == want


def test_one_missed_reading_never_ends_a_season(monkeypatch):
    """Chrome is momentarily busy, or the page is swapping documents. A run that may be hours
    old and cannot resume mid-episode is not endable on a single reading."""
    readings = iter([("gone", ""), ("watch", "https://www.netflix.com/watch/80052801")])
    monkeypatch.setattr(subs, "_tab_state", lambda: next(readings))
    subs._check_tab()  # no raise


def test_a_tab_netflix_bounced_off_watch_is_steered_back_not_declared_closed(monkeypatch):
    """The bug: Netflix navigates its own tab away (player error, expired session, end of a
    title) and the run reported that as a closed tab. The tab is open and the fix is to go
    back, so this raises the *recoverable* error the retry loops already answer with a reload."""
    seen = iter([("away", "https://www.netflix.com/browse")] * 4
                + [("watch", "https://www.netflix.com/watch/42")])
    monkeypatch.setattr(subs, "_tab_state", lambda: next(seen))
    monkeypatch.setattr(subs, "_WATCH_URL", "https://www.netflix.com/watch/42")
    sent = []
    monkeypatch.setattr(subs, "_chrome_tab",
                        lambda action, match=None: sent.append((action, match)) or "ok")

    with pytest.raises(subs.ExportError):
        subs._check_tab()
    # Steered by the wide match: the tab it has to reach is, by definition, not on /watch.
    assert sent and sent[0][1] == NETFLIX_MATCH
    assert "https://www.netflix.com/watch/42" in sent[0][0]


def test_a_tab_that_wont_come_back_stops_with_the_real_reason(monkeypatch):
    monkeypatch.setattr(subs, "_tab_state", lambda: ("away", "https://www.netflix.com/login"))
    monkeypatch.setattr(subs, "_WATCH_URL", "https://www.netflix.com/watch/42")
    monkeypatch.setattr(subs, "_chrome_tab", lambda action, match=None: "ok")

    with pytest.raises(subs._Aborted) as caught:
        subs._check_tab()
    assert "watch page" in caught.value.reason
    assert "netflix.com/login" in caught.value.reason  # says where it actually went


def test_a_title_page_starts_the_player_instead_of_refusing(monkeypatch):
    """`netflix.com/title/<id>` is one navigation from the player, and Netflix resolves
    `/watch/<title id>` to the episode to play — so the run opens it rather than telling the
    user to go and press play on a tab that is already on the right show."""
    monkeypatch.setattr(subs, "_tab_state",
                        lambda: ("away", "https://www.netflix.com/title/81159258"))
    sent = []
    monkeypatch.setattr(subs, "_chrome_tab",
                        lambda action, match=None: sent.append((action, match)) or "ok")
    monkeypatch.setattr(subs, "_run_main", lambda _body: {"ready": True})
    monkeypatch.setattr(subs, "_pause", lambda: None)
    monkeypatch.setattr(subs, "_WATCH_URL", "")

    subs._start_player()
    assert sent == [('set URL of t to "https://www.netflix.com/watch/81159258"', NETFLIX_MATCH)]
    # Recorded, so a tab Netflix later steers away has somewhere to be sent back to.
    assert subs._WATCH_URL == "https://www.netflix.com/watch/81159258"


def test_start_player_leaves_every_other_tab_alone(monkeypatch):
    """A watch tab needs no help, and /browse is preflight's to explain — its message is far
    better than anything a guess from here would produce."""
    touched = []
    monkeypatch.setattr(subs, "_chrome_tab",
                        lambda action, match=None: touched.append(action) or "ok")
    for state, url in [("watch", "https://www.netflix.com/watch/80052801"),
                       ("away", "https://www.netflix.com/browse"),
                       ("gone", "")]:
        monkeypatch.setattr(subs, "_tab_state", lambda _s=state, _u=url: (_s, _u))
        subs._start_player()
    assert touched == []


def test_a_player_that_never_comes_up_is_a_retryable_failure(monkeypatch):
    """Recoverable, not an abort: the tab is there, Netflix was just slow or unhappy."""
    monkeypatch.setattr(subs, "_tab_state",
                        lambda: ("away", "https://www.netflix.com/title/81159258"))
    monkeypatch.setattr(subs, "_chrome_tab", lambda action, match=None: "ok")
    monkeypatch.setattr(subs, "_run_main", lambda _body: {})  # globals never appear
    monkeypatch.setattr(subs, "PLAYER_START_GIVEUP", 0.01)

    with pytest.raises(subs.ExportError):
        subs._start_player()


def test_a_second_netflix_tab_is_refused_and_both_are_named(monkeypatch):
    """"The Netflix tab" only names one tab while there is one. Every step reaches its tab by
    URL substring and takes the first match, and the halves of a run don't even match on the
    same substring — so a second tab is a run driving two pages, not a run picking wrongly
    once."""
    tabs = ["https://www.netflix.com/watch/80052801", "https://www.netflix.com/browse"]
    monkeypatch.setattr(subs, "_netflix_tabs", lambda: tabs)

    with pytest.raises(subs.ExportError) as caught:
        subs._require_one_netflix_tab()
    for url in tabs:  # named, so the one to close is findable without hunting through Chrome
        assert url in str(caught.value)


def test_one_netflix_tab_or_none_starts_normally(monkeypatch):
    """None is not this check's to answer — _preflight_page's message for it is far better."""
    for tabs in ([], ["https://www.netflix.com/watch/80052801"]):
        monkeypatch.setattr(subs, "_netflix_tabs", lambda _t=tabs: _t)
        subs._require_one_netflix_tab()  # no raise


def test_a_chrome_that_wont_answer_is_left_to_preflight(monkeypatch):
    """Chrome not running is a diagnosis this check would only make worse by guessing at."""
    def unreachable():
        raise subs.AppleScriptError("Google Chrome got an error: isn’t running")

    monkeypatch.setattr(subs, "_netflix_tabs", unreachable)
    subs._require_one_netflix_tab()  # no raise


def test_run_refuses_before_it_touches_the_browser(monkeypatch):
    """Ahead of ``_start_player`` on purpose: that is the first thing to navigate a tab by the
    wide match, so it is the first thing that could grab the wrong one."""
    touched = []
    monkeypatch.setattr(subs, "_netflix_tabs",
                        lambda: ["https://www.netflix.com/watch/1",
                                 "https://www.netflix.com/watch/2"])
    monkeypatch.setattr(subs, "_start_player", lambda: touched.append("start_player"))
    monkeypatch.setattr(subs, "_preflight_page", lambda: touched.append("preflight"))

    with pytest.raises(subs.ExportError):
        subs.run("German", "ASR Pro German")
    assert touched == []


def test_netflix_tabs_keeps_every_match_chrome_returned(monkeypatch):
    monkeypatch.setattr(subs, "_as", lambda _src: "https://www.netflix.com/watch/1\n"
                                                  "https://www.netflix.com/title/2\n")
    assert subs._netflix_tabs() == ["https://www.netflix.com/watch/1",
                                    "https://www.netflix.com/title/2"]
    monkeypatch.setattr(subs, "_as", lambda _src: "")
    assert subs._netflix_tabs() == []
    monkeypatch.setattr(subs, "_as", lambda _src: None)  # Chrome answered nothing at all
    assert subs._netflix_tabs() == []


class _NoWakeLock:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _stub_run(monkeypatch, tmp_path, episodes, *, on_tracks):
    """`run()` with the browser removed — the ordering and the banner stay real."""
    monkeypatch.setattr(subs, "_require_one_netflix_tab", lambda: None)
    monkeypatch.setattr(subs, "_start_player", lambda: None)
    monkeypatch.setattr(subs, "_preflight_page", lambda: None)
    monkeypatch.setattr(subs, "_pause_keeper", lambda: None)
    monkeypatch.setattr(subs, "DisplayWakeLock", _NoWakeLock)
    monkeypatch.setattr(subs, "_check_tab", lambda: None)
    monkeypatch.setattr(subs, "_season_episodes",
                        lambda: (episodes, episodes[0]["id"], "Some Show", 1))
    monkeypatch.setattr(subs.settings, "SRT_TARGET_DIR", str(tmp_path))
    monkeypatch.setattr(subs, "_preflight_tracks", on_tracks)
    monkeypatch.setattr(subs, "_get_autopause", lambda: True)
    monkeypatch.setattr(subs, "_set_autopause", lambda _v: None)
    monkeypatch.setattr(subs, "_toggle_lln", lambda _v: True)
    monkeypatch.setattr(subs.library, "update", lambda *_a, **_k: None)
    monkeypatch.setattr(subs, "_export_episode",
                        lambda ep, *_a, **_k: f"E{ep['seq']:02d}.srt")


def test_the_episodes_are_announced_before_language_reactor_is_switched_on(monkeypatch,
                                                                          tmp_path, capsys):
    """LR is the slow half — a cold toggle can sit at LOADING for minutes. Printing the season
    after that wait put the one thing the user is waiting to read behind the wait itself."""
    seen_before_tracks = {}

    def on_tracks(*_a, **_k):
        seen_before_tracks["out"] = capsys.readouterr().out

    _stub_run(monkeypatch, tmp_path,
              [{"seq": n, "id": 100 + n, "title": f"Episode {n}"} for n in (1, 2, 3)],
              on_tracks=on_tracks)

    subs.run("German", "ASR Pro German")
    printed = seen_before_tracks.get("out", "")
    assert "Some Show" in printed              # which show
    assert "1–3" in printed  # and which episodes
    assert "Running in the background" in printed  # the whole banner, not just its first line


def test_nothing_to_rip_never_wakes_language_reactor(monkeypatch, tmp_path):
    """A run that fetches nothing has no reason to spend minutes switching LR on."""
    woken = []
    episodes = [{"seq": 1, "id": 101, "title": "Episode 1"}]
    _stub_run(monkeypatch, tmp_path, episodes, on_tracks=lambda *a, **k: woken.append(a))

    done = tmp_path / "Some Show"
    done.mkdir()
    (done / f"{subs.naming.srt_base('Some Show', 1, 1)} - Primary.srt").write_text("x")

    subs.run("German", "ASR Pro German")
    assert woken == []


def test_a_tab_removed_seconds_after_we_opened_it_is_put_back(monkeypatch):
    """The E16 failure, turned from an ending into a stumble: something removed the tab and the
    whole season stopped. A tab that dies inside the window never had a chance to be closed by
    the user — they had nothing to look at yet — so the run reopens the episode it was on and
    lets the ordinary retry rebuild Language Reactor's state."""
    monkeypatch.setattr(subs, "_REOPENS", 0)
    monkeypatch.setattr(subs, "_WATCH_URL", "https://www.netflix.com/watch/42")
    monkeypatch.setattr(subs, "_LAST_NAV", subs.time.time())
    monkeypatch.setattr(subs, "_chrome_tabs_total", lambda: 30)
    opened = []
    monkeypatch.setattr(subs, "open_tab", opened.append)
    # Gone until it is put back, watch afterwards — so the confirm loop can't accidentally
    # find it before the reopen, which is the whole thing under test.
    monkeypatch.setattr(subs, "_tab_state",
                        lambda: ("watch", "https://www.netflix.com/watch/42") if opened
                        else ("gone", ""))

    with pytest.raises(subs.ExportError):        # recoverable — the episode retries
        subs._check_tab()
    assert opened == ["https://www.netflix.com/watch/42"]


def test_reopening_gives_up_after_the_cap_and_says_what_it_measured(monkeypatch):
    monkeypatch.setattr(subs, "_REOPENS", subs.REOPEN_LIMIT)
    monkeypatch.setattr(subs, "_WATCH_URL", "https://www.netflix.com/watch/42")
    monkeypatch.setattr(subs, "_LAST_NAV", subs.time.time())
    monkeypatch.setattr(subs, "_chrome_tabs_total", lambda: 30)
    monkeypatch.setattr(subs, "_tab_state", lambda: ("gone", ""))

    with pytest.raises(subs._Aborted) as caught:
        subs._check_tab()
    assert "removing netflix.com tabs" in caught.value.reason


def test_closing_the_tab_yourself_still_stops_the_run(monkeypatch):
    """The documented stop gesture. It survives because it lives OUTSIDE the window: a tab you
    close while it sits there playing is one the reopen path never touches."""
    monkeypatch.setattr(subs, "_REOPENS", 0)
    monkeypatch.setattr(subs, "_WATCH_URL", "https://www.netflix.com/watch/42")
    monkeypatch.setattr(subs, "_LAST_NAV", subs.time.time() - subs.TAB_KILL_WINDOW - 1)
    monkeypatch.setattr(subs, "_chrome_tabs_total", lambda: 30)
    monkeypatch.setattr(subs, "_tab_state", lambda: ("gone", ""))
    monkeypatch.setattr(subs, "open_tab", lambda url: pytest.fail("must not reopen"))

    with pytest.raises(subs._Aborted) as caught:
        subs._check_tab()
    assert caught.value.reason == "the Netflix tab was closed"


def test_a_tab_that_dies_much_later_is_reported_plainly(monkeypatch):
    """An hour into a run, a missing tab really is a closed tab — don't cry wolf."""
    monkeypatch.setattr(subs, "_tab_state", lambda: ("gone", ""))
    monkeypatch.setattr(subs, "_chrome_tabs_total", lambda: 30)
    monkeypatch.setattr(subs, "_LAST_NAV", subs.time.time() - subs.TAB_KILL_WINDOW - 1)

    with pytest.raises(subs._Aborted) as caught:
        subs._check_tab()
    assert caught.value.reason == "the Netflix tab was closed"


def test_a_wrong_track_export_restarts_language_reactor_before_the_retry(monkeypatch):
    """Measured on E12: three attempts, each with a reload between them, each exporting the
    same Japanese file — while a run that switched LR off and on came back right on its first
    attempt. So a reload is not the cure for this failure class, and it gets its own."""
    acted = []
    monkeypatch.setattr(subs, "_toggle_lln", lambda on: acted.append(f"lln={on}") or True)
    monkeypatch.setattr(subs, "_reload_page", lambda: acted.append("reload"))

    subs._recover_episode(subs.WrongTrack("wrong track exported — the text is Japanese"))

    assert acted == ["lln=False", "lln=True", "reload"]


def test_every_other_failure_is_still_just_a_reload(monkeypatch):
    """Restarting LR costs seconds and re-loads the track; it must not ride on every stumble."""
    acted = []
    monkeypatch.setattr(subs, "_toggle_lln", lambda on: acted.append(f"lln={on}") or True)
    monkeypatch.setattr(subs, "_reload_page", lambda: acted.append("reload"))

    subs._recover_episode(subs.ExportError("stalled"))

    assert acted == ["reload"]


def test_a_restart_that_fails_never_costs_the_episode(monkeypatch):
    """Best-effort: the retry that follows is what actually matters."""
    monkeypatch.setattr(subs, "_toggle_lln",
                        lambda on: (_ for _ in ()).throw(subs.AppleScriptError("no tab")))
    monkeypatch.setattr(subs, "_reload_page", lambda: None)
    subs._recover_episode(subs.WrongTrack("wrong track exported"))  # no raise


def test_the_wrong_track_reason_is_a_contract_not_a_sentence():
    """`gigaku subs` recognises this failure class by the prefix, so the two sides are pinned
    together — a reworded message would otherwise silently stop matching."""
    from lib.subs import excel_to_srt
    assert excel_to_srt.WRONG_TRACK == "wrong track exported"
    assert issubclass(subs.WrongTrack, subs.ExportError)  # still caught by every handler


class _FakeGlosser:
    """Records how the run ended rather than doing any work."""

    calls: list[bool] = []

    def __init__(self):
        _FakeGlosser.calls = []

    def submit(self, *_a):
        pass

    def finish(self, wait=True):
        _FakeGlosser.calls.append(wait)


def test_a_finished_rip_waits_for_the_glosser(monkeypatch, tmp_path):
    """The browser's part is over, so the last episodes' Russian arrives with the tab already
    handed back — but the run must not print its summary before it has arrived."""
    monkeypatch.setattr(subs, "_Glosser", _FakeGlosser)
    _stub_run(monkeypatch, tmp_path, [{"seq": 1, "id": 101, "title": "One"}],
              on_tracks=lambda *a, **k: None)

    subs.run("German", "ASR Pro German")

    assert _FakeGlosser.calls == [True]


def test_a_stopped_rip_does_not(monkeypatch, tmp_path):
    """Ctrl-C shouldn't sit through an episode nobody is waiting for; the work file resumes."""
    monkeypatch.setattr(subs, "_Glosser", _FakeGlosser)
    _stub_run(monkeypatch, tmp_path, [{"seq": 1, "id": 101, "title": "One"}],
              on_tracks=lambda *a, **k: None)
    monkeypatch.setattr(subs, "_export_episode",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

    subs.run("German", "ASR Pro German")

    assert _FakeGlosser.calls == [False]


def test_abort_reason_is_carried_not_guessed():
    """The ending line prints ``reason``; the default only covers the case it describes."""
    assert subs._Aborted().reason == "the Netflix tab was closed"
    assert subs._Aborted("Chrome quit").reason == "Chrome quit"
