"""The rip and the gloss are two stages of a pipeline, not two halves of one episode.

They share nothing — ripping is Chrome and Language Reactor, glossing is `claude -p` against a
Primary already on disk — so run in sequence they idled in turn: measured on The Eminence in
Shadow, the browser sat still through ~4 minutes of Opus requests and the glosser sat still
through the next episode's ASR wait. These pin the hand-off, and what happens to the queue when
a run ends either way.

The browser is monkeypatched away entirely; only ``_Glosser`` and ``lib.config.noting`` are real.
"""
import threading

from lib import config
from lib.subs import subs


def test_the_rip_does_not_wait_for_the_gloss(monkeypatch):
    """The whole point: submit returns while the gloss is still running, so the next episode's
    rip starts now. If this ever blocks, the pipeline has quietly become a sequence again."""
    holding = threading.Event()
    started = threading.Event()

    def slow_gloss(primary, secondary, tag=""):
        started.set()
        holding.wait(5)

    monkeypatch.setattr(subs, "_gloss", slow_gloss)
    glosser = subs._Glosser()
    glosser.submit(subs.Path("E01.srt"), subs.Path("E01.ru.srt"), "E01")

    assert started.wait(5), "the worker never picked the episode up"
    # …and here we are, back in the caller, with the gloss still in flight.
    assert not holding.is_set()
    holding.set()
    glosser.finish()


def test_the_index_is_written_when_the_PAIR_exists_not_when_the_rip_ends(monkeypatch):
    """`library.scan` indexes a Primary+Secondary pair and nothing else, and `library.update`
    drops any id whose episode the scan didn't find. Registering after the rip — which is when
    only the Primary exists — therefore loses the Netflix id silently, and the extension falls
    back from an exact id match to title+season+episode. Measured: five episodes indexed with
    no id at all after the glosser went parallel."""
    order = []
    monkeypatch.setattr(subs, "_gloss",
                        lambda p, s, tag="": order.append(f"gloss {tag}") or True)
    glosser = subs._Glosser()
    glosser.submit(subs.Path("E01.srt"), subs.Path("E01.ru.srt"), "E01",
                   on_done=lambda: order.append("index E01"))

    glosser.finish()

    assert order == ["gloss E01", "index E01"]


def test_a_failed_gloss_never_indexes_a_pair_that_isnt_there(monkeypatch):
    indexed = []
    monkeypatch.setattr(subs, "_gloss", lambda p, s, tag="": False)   # Secondary not written
    glosser = subs._Glosser()
    glosser.submit(subs.Path("E01.srt"), subs.Path("E01.ru.srt"), "E01",
                   on_done=lambda: indexed.append("E01"))

    glosser.finish()

    assert indexed == []


def test_index_hands_the_id_to_the_library(monkeypatch):
    seen = {}
    monkeypatch.setattr(subs.library, "update",
                        lambda root, ids=None: seen.update(root=root, ids=ids))

    subs._index(subs.Path("/subs"), {"id": 81642109, "seq": 12}, "Some Show", 1)

    assert seen == {"root": "/subs", "ids": {81642109: ("Some Show", 1, 12)}}


def test_finish_drains_everything_queued(monkeypatch):
    glossed = []
    monkeypatch.setattr(subs, "_gloss", lambda p, s, tag="": glossed.append(tag))
    glosser = subs._Glosser()
    for n in range(1, 6):
        glosser.submit(subs.Path(f"E{n}.srt"), subs.Path(f"E{n}.ru.srt"), f"E{n:02d}")

    glosser.finish()

    # In order: one worker, so the episodes are glossed in the order they were ripped.
    assert glossed == ["E01", "E02", "E03", "E04", "E05"]


def test_a_stopped_run_walks_away_from_the_queue(monkeypatch, capsys):
    """Ctrl-C shouldn't sit through four more minutes of an episode nobody is waiting for.
    Every answered request is already in the work file, so `gigaku translate` resumes."""
    holding = threading.Event()
    monkeypatch.setattr(subs, "_gloss", lambda p, s, tag="": holding.wait(5))
    glosser = subs._Glosser()
    for n in (1, 2, 3):
        glosser.submit(subs.Path(f"E{n}.srt"), subs.Path(f"E{n}.ru.srt"), f"E{n:02d}")

    glosser.finish(wait=False)   # returns even though the worker is blocked

    assert "gigaku translate" in capsys.readouterr().out
    holding.set()


def test_one_bad_gloss_never_takes_the_worker_with_it(monkeypatch):
    """A dead worker would silently strand every later episode — the failure that a
    `while True: get()` loop invites."""
    seen = []

    def gloss(primary, secondary, tag=""):
        seen.append(tag)
        if tag == "E01":
            raise RuntimeError("boom")

    monkeypatch.setattr(subs, "_gloss", gloss)
    monkeypatch.setattr(subs, "_UNGLOSSED", [])
    glosser = subs._Glosser()
    glosser.submit(subs.Path("E01.srt"), subs.Path("E01.ru.srt"), "E01")
    glosser.submit(subs.Path("E02.srt"), subs.Path("E02.ru.srt"), "E02")

    glosser.finish()

    assert seen == ["E01", "E02"]
    assert subs._UNGLOSSED == ["E01.srt"]   # named, so the run's ending can report it


def test_never_started_is_never_joined(monkeypatch):
    """A run with nothing to fetch must not create — or wait on — a thread."""
    glosser = subs._Glosser()
    glosser.finish()          # no raise, no hang
    assert not glosser._started


def test_a_gloss_line_says_which_episode_it_is_from(capsys):
    """Two jobs narrate at once now, and 'request 3/7' alone can't say whose it is."""
    with config.noting("E14"):
        config.note("  request 3/7")
    config.note("  request 1/6")

    err = capsys.readouterr().err.splitlines()
    assert err == ["E14 request 3/7", "  request 1/6"]


def test_the_tag_is_per_thread(capsys):
    """The rip narrates from the main thread while the glosser narrates from its own — a
    global would have each stamping the other's lines."""
    done = threading.Event()

    def worker():
        with config.noting("E14"):
            config.note("from the glosser")
        done.set()

    threading.Thread(target=worker).start()
    assert done.wait(5)
    config.note("from the rip")

    err = capsys.readouterr().err.splitlines()
    assert err == ["E14 from the glosser", "from the rip"]


def test_no_translate_never_spells(monkeypatch):
    """`--no-translate` is documented as the path that needs no `claude` on PATH, and the
    proofreader is a `claude` call. It is unreachable there **structurally** — `_gloss` is the
    only place it runs and `_export_episode` only reaches `_gloss` under `if glossing:` — so
    this pins the shape rather than a second switch that could drift out of step."""
    import inspect

    from lib.subs import subs

    source = inspect.getsource(subs)
    calls = [line for line in source.splitlines() if "spell.episode(" in line]
    assert calls, "the proofreader is not called at all"
    assert "spell.episode(" in inspect.getsource(subs._gloss)
    assert "spell.episode(" not in inspect.getsource(subs._export_episode)
