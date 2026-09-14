"""Language Reactor reverting the subtitle track mid-episode.

LR does not merely fail to switch a track — it switches, then reverts to the title's default
on its own clock. The revert used to be caught only by the verify *after* the ASR wait and the
translation wait, i.e. after LR had fetched a translation for every line of the wrong track;
all of it was thrown away and the whole wait paid for twice. These pin that both long waits
now notice it on the tick it happens.

Importing lib.subs.subs costs pyobjc; the browser itself is monkeypatched away.
"""
import pytest

from lib.subs import subs


def _asr(n, mt=None, *, type_="asr", name="German", **extra):
    d = {"n": n, "tmType": type_, "tmName": name, **extra}
    if mt is not None:
        d["mt"] = mt
    return d


@pytest.fixture
def watch():
    return subs._TrackWatch("audio-id", "sub-id", "German", True)


@pytest.fixture
def browser(monkeypatch):
    """`_run_main` answers from a script of readings; every re-select is recorded."""
    monkeypatch.setattr(subs.time, "sleep", lambda _s: None)
    monkeypatch.setattr(subs, "_check_tab", lambda: None)
    monkeypatch.setattr(subs, "_translation_rate_limited", lambda: False)
    clicks = []
    monkeypatch.setattr(subs, "_click_tracks", lambda aid, sid: clicks.append((aid, sid)))

    def script(readings):
        it = iter(readings)
        last = {}

        def run_main(body):
            nonlocal last
            if "setupMTranslations" in body:
                return {"k": 1}  # the nudge, not a reading
            last = next(it, last)  # hold the final reading if the wait polls once more
            return last

        monkeypatch.setattr(subs, "_run_main", run_main)
        return clicks

    return script


def test_drift_is_the_wrong_track_not_a_track_still_loading(watch):
    assert watch.drifted({"tmType": "asr", "tmName": "German"}) is False
    assert watch.drifted({"tmType": "asr", "tmName": "german"}) is False  # case is not a drift
    assert watch.drifted({"tmType": "closedcaptions", "tmName": "Korean"}) is True
    # Same type, wrong language: LR will happily land on ASR Korean [Original].
    assert watch.drifted({"tmType": "asr", "tmName": "Korean [Original]"}) is True
    # Nothing loaded yet is LR still working, not a revert.
    assert watch.drifted({"tmType": None}) is False
    assert watch.drifted({}) is False


def test_the_guard_reselects_only_when_the_track_actually_moved(watch, browser, monkeypatch):
    clicks = browser([])
    watch.guard({"tmType": "asr", "tmName": "German"})
    watch.guard({"tmType": None})
    assert clicks == []
    watch.guard({"tmType": "closedcaptions", "tmName": "Korean"})
    assert clicks == [("audio-id", "sub-id")]


def test_reselecting_forever_is_not_an_option(watch, browser):
    """An LR that reverts every few seconds never stalls either — an oscillating count reads
    as progress — so the repair is bounded and hands the episode to the retry loop."""
    browser([])
    korean = {"tmType": "closedcaptions", "tmName": "Korean"}
    for _ in range(subs.DRIFT_LIMIT):
        watch.guard(korean)
    with pytest.raises(subs.ExportError):
        watch.guard(korean)


def test_translations_complete_on_the_wrong_track_are_not_complete(watch, browser):
    """The wait that used to burn. A revert here means LR translates the whole of the default
    track — nothing stalls, the counts climb, `mt >= n` reads as finished."""
    clicks = browser([
        _asr(800, 800, type_="closedcaptions", name="Korean"),  # wrong track, fully translated
        _asr(1200, 600),                                        # re-selected; the real track
        _asr(1200, 1200),
        _asr(1200, 1200),                                       # complete twice running
    ])

    out = subs._wait_translations(watch)

    assert clicks == [("audio-id", "sub-id")]  # caught on the first tick, repaired once
    assert out["tmType"] == "asr" and out["mt"] == 1200


def test_asr_coverage_is_not_believed_from_the_default_track(watch, browser):
    """The title's own track is complete from the first second, so a revert would satisfy the
    coverage check instantly and hand back a "finished" episode of the wrong language."""
    clicks = browser([
        _asr(900, type_="closedcaptions", name="Korean", durMs=1_000_000, lastMs=1_000_000),
        _asr(400, durMs=1_000_000, lastMs=300_000),   # the real track, still generating
        _asr(1100, durMs=1_000_000, lastMs=900_000),  # reaches the end
    ])

    out = subs._wait_asr_complete(watch)

    assert clicks == [("audio-id", "sub-id")]
    assert out["tmType"] == "asr" and out["lastMs"] == 900_000
