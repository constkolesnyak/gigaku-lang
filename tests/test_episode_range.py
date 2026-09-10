"""The `gigaku subs 3-12` range — the part that decides how many hours the rip runs for."""
import pytest

from lib.config import UserError
from lib.subs import episode_range


def eps(*seqs):
    return [{"seq": s, "id": 1000 + s, "title": f"E{s}"} for s in seqs]


def test_the_three_forms():
    assert episode_range.parse("3-12") == episode_range.Range(3, 12, "3-12")
    assert episode_range.parse("3-") == episode_range.Range(3, None, "3-")
    assert episode_range.parse("-3") == episode_range.Range(None, 3, "-3")
    assert episode_range.parse("1-1") == episode_range.Range(1, 1, "1-1")


def test_no_range_means_the_whole_season():
    assert episode_range.parse(None) is None
    assert episode_range.select(eps(1, 2, 3), None, "Dark season 1") == eps(1, 2, 3)


@pytest.mark.parametrize("text", ["3--12", "-", "abc-", "3-4-5", "3,12", "", "3 12"])
def test_a_bad_shape_shows_every_accepted_form(text):
    """The range is typed by hand before an hours-long rip. A rejection that doesn't teach
    the syntax just costs another wrong guess, so pin that all three forms are named."""
    with pytest.raises(UserError) as exc:
        episode_range.parse(text)
    for form in ("3-12", "3-", "-3"):
        assert form in str(exc.value)


def test_a_bare_number_is_refused_as_ambiguous():
    """`5` reads as "from 5 on" as naturally as "just 5" — guessing either would silently
    rip the wrong half of a season."""
    with pytest.raises(UserError) as exc:
        episode_range.parse("5")
    assert "5-" in str(exc.value) and "-5" in str(exc.value) and "5-5" in str(exc.value)


@pytest.mark.parametrize("text", ["0-3", "3-0", "0-0"])
def test_there_is_no_episode_zero(text):
    with pytest.raises(UserError, match="numbered from 1"):
        episode_range.parse(text)


def test_a_reversed_range_suggests_the_swap():
    with pytest.raises(UserError, match="2-3"):
        episode_range.parse("3-2")


def test_select_filters_by_seq():
    season = eps(*range(1, 11))
    assert [e["seq"] for e in episode_range.select(season, episode_range.parse("3-"), "S")] \
        == [3, 4, 5, 6, 7, 8, 9, 10]
    assert [e["seq"] for e in episode_range.select(season, episode_range.parse("-3"), "S")] \
        == [1, 2, 3]
    assert [e["seq"] for e in episode_range.select(season, episode_range.parse("3-5"), "S")] \
        == [3, 4, 5]


def test_select_is_netflix_numbering_not_a_list_index():
    """`seq` is the E03 in the filename, not a position — so a season that doesn't start at
    1 must still answer `5-6` with episodes 5 and 6. `episodes[start-1:end]` would not."""
    chosen = episode_range.select(eps(5, 6, 7), episode_range.parse("5-6"), "S")
    assert [e["seq"] for e in chosen] == [5, 6]


def test_select_names_what_the_season_actually_has():
    season = eps(*range(1, 9))
    with pytest.raises(UserError) as exc:
        episode_range.select(season, episode_range.parse("4-9999999999"), "Dark season 2")
    assert "1–8" in str(exc.value) and '"4-"' in str(exc.value)

    with pytest.raises(UserError) as exc:
        episode_range.select(season, episode_range.parse("12-20"), "Dark season 2")
    assert "1–8" in str(exc.value)


def test_an_open_end_is_never_out_of_range():
    """`3-` says "to the end", so it can't overshoot however short the season is."""
    chosen = episode_range.select(eps(1, 2, 3), episode_range.parse("3-"), "S")
    assert [e["seq"] for e in chosen] == [3]


def test_spans_collapse_consecutive_episodes():
    assert episode_range.spans([1, 2, 3, 7, 8]) == "1–3, 7–8"
    assert episode_range.spans([5]) == "5"
    assert episode_range.spans([1, 3, 5]) == "1, 3, 5"
    assert episode_range.spans([]) == ""


def test_the_banner_does_not_say_the_same_thing_twice():
    """Nothing on disk yet, so "Requested 3-12  (10 episodes)" is the row above it reworded —
    and the block has to be readable at a glance."""
    season = eps(*range(1, 17))
    rng = episode_range.parse("3-12")
    chosen = episode_range.select(season, rng, "S")

    assert episode_range.describe(chosen, chosen, len(season), 2, rng)[1] == [
        ("Ripping", "3–12  (10 episodes)")]


def test_the_banner_promises_what_will_actually_be_downloaded():
    """It announces the episodes it will *fetch*, not the ones that were asked for. On a
    resumed season those differ, and "3–12" that then skips four of them is a promise
    about work nothing is going to do."""
    season = eps(*range(1, 17))
    rng = episode_range.parse("3-12")
    chosen = episode_range.select(season, rng, "S")
    todo = [e for e in chosen if e["seq"] not in (3, 4, 7, 8)]   # those four on disk already

    headline, rows = episode_range.describe(todo, chosen, len(season), 2, rng)

    assert headline == "season 2, 16 episodes"
    assert rows == [
        ("Ripping", "5–6, 9–12  (6 episodes)"),
        ("Requested", "3-12  (10 episodes)"),
        ("Already done", "3–4, 7–8"),
    ]


def test_the_banner_leaves_out_rows_with_nothing_to_say():
    """No range asked for and nothing on disk yet — the block is one row, not three empty
    ones."""
    season = eps(*range(1, 9))
    assert episode_range.describe(season, season, 8, 2, None) == (
        "season 2, 8 episodes", [("Ripping", "1–8  (8 episodes)")])
    assert episode_range.describe(season[4:5], season[4:5], 8, 2, None)[1] == [
        ("Ripping", "5  (1 episode)")]


def test_the_banner_says_so_when_there_is_nothing_left_to_rip():
    season = eps(*range(1, 9))
    _, rows = episode_range.describe([], season, 8, 2, None)
    assert rows == [("Ripping", "nothing — everything asked for is already there"),
                    ("Already done", "1–8")]


def test_the_banner_does_not_number_a_film():
    """A film has one synthetic episode and `srt_base` gives it no E-number, so promising
    "E01" would name a file that never gets written."""
    film = eps(1)
    assert episode_range.describe(film, film, 1, None, None) == (
        "film", [("Ripping", "the film")])
    assert episode_range.describe([], film, 1, None, None)[1] == [
        ("Ripping", "nothing — already ripped")]


def test_a_gap_in_seq_is_not_a_silent_empty_run():
    season = eps(1, 2, 3, 4, 8)
    with pytest.raises(UserError, match="no episode between 5 and 7"):
        episode_range.select(season, episode_range.parse("5-7"), "S")
    assert [e["seq"] for e in episode_range.select(season, episode_range.parse("3-8"), "S")] \
        == [3, 4, 8]


def test_contains_filters_a_known_set():
    """`gigaku translate` filters the library by number, where `select` cannot serve: it
    validates against a season Netflix just described and refuses an overshoot."""
    assert [n for n in range(1, 8) if episode_range.parse("3-5").contains(n)] == [3, 4, 5]
    assert [n for n in range(1, 5) if episode_range.parse("3-").contains(n)] == [3, 4]
    assert [n for n in range(1, 5) if episode_range.parse("-2").contains(n)] == [1, 2]
