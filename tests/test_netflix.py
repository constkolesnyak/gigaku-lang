"""The Netflix gallery rip and the IMDb join — everything that can be tested without Chrome."""
import json
import re

import pytest

from lib.config import UserError, settings
from lib.netflix import gallery, imdb, markers, store, titles, watched


def item(title="Dark", vid=80100172, kind="show", year=2017, **extra):
    """One Netflix `itemSummary`, shaped like the real thing."""
    base = {
        "id": vid, "videoId": vid, "unifiedEntityId": f"Video:{vid}", "title": title,
        "type": kind, "releaseYear": year, "isOriginal": True,
        "maturity": {"rating": {"value": "16", "maturityDescription": "Suitable for ages 16 and up",
                                "board": "Germany", "maturityLevel": 100}},
        "availability": {"isPlayable": True},
        "boxArt": {"url": "https://occ.nflxso.net/art.jpg"},
    }
    base.update(extra)
    return base


def slot(**kwargs):
    return {"itemSummary": item(**kwargs), "queue": {"inQueue": False}, "inRemindMeList": False}


# ── the record shape ─────────────────────────────────────────────────────────

def test_a_slot_past_the_end_of_the_gallery_is_not_a_record():
    """Falcor materialises indexes beyond the list as present-but-empty instead of erroring,
    so "no itemSummary" is the only signal the rip has run out of gallery."""
    assert store.record(4400, {}) is None
    assert store.record(4400, {"itemSummary": {}}) is None  # present but hollow
    assert store.record(4400, {"availability": {"isPlayable": True}}) is None  # no title


def test_a_series_and_a_film_project_their_own_shape():
    show = store.record(0, slot(title="Dark", kind="show", seasonCount=3, episodeCount=26,
                                numSeasonsLabel="3 Seasons"))
    film = store.record(1, slot(title="Blade Runner 2049", vid=80999998, kind="movie", year=2017))

    assert (show["title"], show["type"], show["seasons"], show["episodes"]) == ("Dark", "show", 3, 26)
    assert show["maturity"] == "16" and show["maturity_board"] == "Germany"
    assert show["nf_url"] == "https://www.netflix.com/title/80100172"
    assert film["seasons"] is None and film["type"] == "movie"
    # The whole itemSummary is kept: a field nobody projected is still there next month.
    assert show["raw"]["unifiedEntityId"] == "Video:80100172"


def test_the_flat_row_has_every_column_and_no_none():
    """The CSV header *is* FLAT_COLUMNS, so a row must fill it exactly — a missing key would
    shift every later cell in the file."""
    row = store.flat(store.record(0, slot()))
    assert list(row) == store.FLAT_COLUMNS
    assert None not in row.values()


def test_in_my_list_comes_from_the_queue_not_the_item():
    plain = store.record(0, slot())
    queued = store.record(0, {**slot(), "queue": {"inQueue": True}})
    assert plain["in_my_list"] is False and queued["in_my_list"] is True


# ── the falcor envelope ──────────────────────────────────────────────────────

def test_parse_window_keeps_titles_and_ignores_falcors_own_keys():
    envelope = {"json": {"genres": {"81510840": {"su": {
        "0": slot(title="Dark"),
        "1": slot(title="Kill Bill: Vol. 1", vid=70016000, kind="movie", year=2003),
        "2": {},                      # past the end
        "$__path": ["genres", 81510840, "su"],  # falcor bookkeeping, not a title
    }}}}}
    got = gallery.parse_window(envelope, "81510840")

    assert sorted(got) == [0, 1]
    assert got[1]["title"] == "Kill Bill: Vol. 1"


def test_an_empty_window_is_how_the_rip_learns_it_reached_the_end():
    envelope = {"json": {"genres": {"81510840": {"su": {"4400": {}, "4401": {}}}}}}
    assert gallery.parse_window(envelope, "81510840") == {}


def test_a_window_is_read_from_the_sublist_that_was_asked_for():
    """`az` and `su` are different lists on the same node, and the envelope names which."""
    envelope = {"json": {"genres": {"81510840": {"az": {"0": slot(title="#realityhigh")}}}}}
    assert gallery.parse_window(envelope, "81510840", "az")[0]["title"] == "#realityhigh"
    assert gallery.parse_window(envelope, "81510840") == {}  # su was not in this answer


# ── merging the node's lists ─────────────────────────────────────────────────

def test_a_title_in_two_lists_stays_one_record_and_keeps_the_storefronts_rank():
    """Merged by Netflix id, because the same title sits at a different index in every list."""
    records = {}
    gallery.merge(records, {3: store.record(3, slot(title="Dark"))}, "az")
    gallery.merge(records, {11: store.record(11, slot(title="Dark"))}, "su")

    assert list(records) == ["80100172"]
    assert records["80100172"]["rank"] == 11             # su's order is the one kept
    assert records["80100172"]["sublists"] == ["az", "su"]


def test_only_a_title_the_storefront_never_listed_is_tagged_new():
    """`discovery` is the page's NEW pill. A title `su` shows is not news, however many other
    lists also found it — otherwise the tag would mark the whole gallery."""
    records = {}
    gallery.merge(records, {0: store.record(0, slot(title="Dark"))}, "su")
    gallery.merge(records, {0: store.record(0, slot(title="Dark"))}, "za")
    gallery.merge(records, {1: store.record(1, slot(title="Hidden", vid=999))}, "az")

    assert records["80100172"]["discovery"] == ""
    assert records["999"]["discovery"] == "az"
    assert store.flat(records["999"])["discovery"] == "az"  # and it reaches the CSV


def test_an_older_dump_is_adopted_as_the_storefront_instead_of_forcing_a_re_rip():
    """The first rip only knew `su` and wrote neither field. Re-walking 4,000 titles to learn
    what the dump already implies would be the wrong trade."""
    old = store.record(0, slot(title="Dark"))
    old.pop("discovery"), old.pop("sublists", None)

    records, progress, done = gallery.adopt(
        {"complete": True, "next_from": 4100, "records": [old]})

    assert records["80100172"]["sublists"] == ["su"]
    assert records["80100172"]["discovery"] == ""   # already listed, so not NEW
    assert progress == {"su": 4100} and done == {"su"}


def test_a_gallery_url_yields_its_id_language_and_a_tab_match_that_cannot_grab_a_watch_tab():
    genre, lang, match = gallery.parse_url("https://www.netflix.com/browse/audio/81510840/de")
    assert (genre, lang) == ("81510840", "de")
    assert match == "netflix.com/browse/audio/81510840"
    assert "netflix.com/watch" not in match  # a plain "netflix.com" would race an open episode

    genre, lang, match = gallery.parse_url("https://www.netflix.com/browse/subtitles/81510840")
    assert (genre, lang, match) == ("81510840", "", "netflix.com/browse/subtitles/81510840")

    with pytest.raises(UserError):
        gallery.parse_url("https://www.netflix.com/watch/80100172")


# ── title normalisation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("Dark", "dark"),
    ("Männer, die auf Ziegen starren", "manner die auf ziegen starren"),  # umlaut folds
    ("Straße der Verdammnis", "strasse der verdammnis"),                  # ß has no NFKD form
    ("Fast & Furious", "fast and furious"),
    ("Amélie", "amelie"),
    ("WALL·E", "wall e"),
    ("Kill Bill: Vol. 1", "kill bill vol 1"),
    ("  spaced   out  ", "spaced out"),
])
def test_normalising_a_title_ignores_case_accents_and_punctuation(raw, expected):
    assert imdb.norm(raw) == expected


def test_key_variants_reach_a_title_filed_differently_on_imdb():
    keys = imdb.keys("Stranger Things: Season 4")
    assert keys[imdb.norm("Stranger Things: Season 4")] == 1.0   # whole title scores most
    assert imdb.norm("Stranger Things") in keys                  # subtitle dropped
    assert all(0 < w <= 1.0 for w in keys.values())

    # A leading article is a *separate, weaker* key, never a silent rewrite.
    article = imdb.keys("Der Pate")
    assert article[imdb.norm("Der Pate")] == 1.0
    assert article[imdb.norm("Pate")] < 1.0

    # Netflix disambiguates with a parenthetical that IMDb doesn't file: "The Office (U.S.)".
    office = imdb.keys("The Office (U.S.)")
    assert imdb.norm("The Office") in office

    assert imdb.keys("") == {}


def test_a_chosen_candidate_is_never_labelled_none():
    """`source: suggestion, confidence: none` claimed a match and no match in the same row.
    "none" now means exactly one thing: nothing was attached."""
    assert imdb.confidence(imdb.ACCEPT_SUGGEST) == "guess"
    assert imdb.confidence(imdb.ACCEPT_BASICS) == "weak"
    assert "none" not in {imdb.confidence(v) for v in range(0, 101)}


# ── scoring ──────────────────────────────────────────────────────────────────

def cand(primary="Dark", kind="tvSeries", start="2017", end="2020", runtime="", genres="Drama"):
    return {"tconst": "tt5753856", "titleType": kind, "primaryTitle": primary,
            "originalTitle": primary, "startYear": start, "endYear": end,
            "runtimeMinutes": runtime, "genres": genres}


def test_a_title_that_shares_no_key_scores_nothing_at_all():
    rec = {"title": "Dark", "type": "show", "year": 2017}
    assert imdb.score(rec, cand(primary="Bright"), imdb.keys("Dark")) is None


def test_a_netflix_show_year_inside_the_imdb_run_is_a_hit_not_a_miss():
    """Netflix reports a series' *newest* season as releaseYear — The Handmaid's Tale reads
    2025 against an IMDb startYear of 2017 — so year-inside-the-run has to count."""
    rec = {"title": "The Handmaid's Tale", "type": "show", "year": 2025}
    inside = cand(primary="The Handmaid's Tale", start="2017", end="2025")
    keys = imdb.keys("The Handmaid's Tale")

    assert imdb.confidence(imdb.score(rec, inside, keys)) in ("exact", "strong")


def test_the_wrong_medium_is_penalised_hard_enough_to_lose():
    """A film sharing a series' name is the classic wrong match; type has to outweigh a
    perfect title hit."""
    rec = {"title": "Dark", "type": "show", "year": 2017}
    keys = imdb.keys("Dark")
    series = imdb.score(rec, cand(kind="tvSeries"), keys)
    film = imdb.score(rec, cand(kind="movie", start="2017", end=""), keys)

    assert series > film
    assert film < imdb.ACCEPT_BASICS  # not merely worse — not good enough to attach at all


def test_a_different_film_of_the_same_name_is_rejected_not_attached_weakly():
    """"Rich in Love" (2020, Brazilian) was matching a 1992 film of the same name and shipping
    as a weak match. A film's year is the one thing Netflix and IMDb agree on, so a big gap
    has to fail the accept threshold — the title then goes to the suggestion endpoint."""
    rec = {"title": "Rich in Love", "type": "movie", "year": 2020}
    keys = imdb.keys("Rich in Love")
    right = imdb.score(rec, cand(primary="Rich in Love", kind="movie", start="2020", end=""), keys)
    wrong = imdb.score(rec, cand(primary="Rich in Love", kind="movie", start="1992", end=""), keys)

    assert right >= imdb.ACCEPT_BASICS
    assert wrong < imdb.ACCEPT_BASICS


def test_a_long_running_series_still_matches_across_a_wide_year_gap():
    """The steeper film penalty must not catch series: Netflix reports the newest season."""
    rec = {"title": "One Piece", "type": "show", "year": 2026}
    got = imdb.score(rec, cand(primary="One Piece", kind="tvSeries", start="1999", end=r"\N"),
                     imdb.keys("One Piece"))
    assert got >= imdb.ACCEPT_BASICS


# ── the join end to end, without touching the network ────────────────────────

@pytest.fixture
def fake_datasets(monkeypatch, tmp_path):
    """enrich() reads rows through _rows(), so the tests hand it lists instead of gzip files."""
    basics = [
        {"tconst": "tt5753856", "titleType": "tvSeries", "primaryTitle": "Dark",
         "originalTitle": "Dark", "startYear": "2017", "endYear": "2020",
         "runtimeMinutes": "60", "genres": "Crime,Drama,Mystery"},
        {"tconst": "tt0000001", "titleType": "movie", "primaryTitle": "Dark",
         "originalTitle": "Dark", "startYear": "1979", "endYear": r"\N",
         "runtimeMinutes": "90", "genres": "Horror"},
        {"tconst": "tt1375666", "titleType": "movie", "primaryTitle": "Inception",
         "originalTitle": "Inception", "startYear": "2010", "endYear": r"\N",
         "runtimeMinutes": "148", "genres": "Action,Sci-Fi"},
    ]
    ratings = [
        {"tconst": "tt5753856", "averageRating": "8.7", "numVotes": "420000"},
        {"tconst": "tt0000001", "averageRating": "9.9", "numVotes": "12"},
        {"tconst": "tt1375666", "averageRating": "8.8", "numVotes": "2500000"},
    ]
    monkeypatch.setattr(imdb, "ensure", lambda refresh=False: {
        "basics": "basics", "ratings": "ratings", "suggest": str(tmp_path / "suggest.json")})
    monkeypatch.setattr(imdb, "_rows", lambda path: iter(basics if path == "basics" else ratings))
    return basics, ratings


def test_the_join_picks_the_series_over_the_same_named_film(fake_datasets):
    records = [store.record(0, slot(title="Dark", kind="show", year=2017))]
    imdb.enrich(records, use_suggest=False)
    rec = records[0]

    assert rec["imdb_id"] == "tt5753856"          # the series, not the 1979 horror film
    assert rec["imdb_rating"] == 8.7
    assert rec["imdb_genres"] == ["Crime", "Drama", "Mystery"]
    assert rec["match_source"] == "basics"
    assert rec["match_confidence"] in ("exact", "strong")
    # Netflix's endpoint refuses to hand over a runtime, so IMDb's fills the column.
    assert rec["runtime_min"] == 60


def test_an_unmatched_title_survives_with_nulls_and_says_so(fake_datasets):
    records = [store.record(0, slot(title="Something Nobody Filmed", vid=1, kind="movie",
                                    year=2011))]
    imdb.enrich(records, use_suggest=False)
    rec = records[0]

    assert rec["imdb_id"] is None and rec["imdb_rating"] is None
    assert rec["match_source"] == "none" and rec["match_confidence"] == "none"
    assert rec["match_note"]  # the page's Unmatched tab shows this


def test_the_weighted_score_ranks_a_landslide_above_a_fluke(fake_datasets, monkeypatch):
    """9.9 from 12 votes must not outrank 8.8 from 2.5M — that is the whole point of the
    shrunk mean, and the reason the page sorts on it by default."""
    monkeypatch.setattr(settings, "IMDB_BAYES", "2000,7.0")
    records = [
        store.record(0, slot(title="Dark", vid=1, kind="movie", year=1979)),
        store.record(1, slot(title="Inception", vid=2, kind="movie", year=2010)),
    ]
    imdb.enrich(records, use_suggest=False)
    fluke, landslide = records

    assert fluke["imdb_rating"] > landslide["imdb_rating"]      # raw average says otherwise
    assert fluke["weighted"] < landslide["weighted"]            # weighted gets it right


def test_the_suggestion_endpoint_is_only_asked_about_what_the_datasets_missed(fake_datasets):
    calls = []

    def fetch(url):
        calls.append(url)
        return {"d": [{"id": "tt9999999", "l": "Akame ga Kill!", "y": 2014, "qid": "tvSeries"}]}

    records = [
        store.record(0, slot(title="Dark", kind="show", year=2017)),        # matches locally
        store.record(1, slot(title="Akame ga Kill!", vid=2, kind="show", year=2014)),
    ]
    imdb.enrich(records, use_suggest=True, fetch=fetch)

    assert len(calls) == 1  # only the miss cost a request
    assert records[1]["imdb_id"] == "tt9999999"
    assert records[1]["match_source"] == "suggestion"


def test_turning_the_suggestion_endpoint_off_makes_no_requests(fake_datasets):
    def fetch(url):
        raise AssertionError("must not be called")

    records = [store.record(0, slot(title="Nothing Here", vid=9, kind="movie", year=2001))]
    imdb.enrich(records, use_suggest=False, fetch=fetch)
    assert records[0]["match_source"] == "none"


# ── the watched list ─────────────────────────────────────────────────────────

def test_the_extensions_backup_file_is_one_video_id_per_line(tmp_path):
    """Netflix Watched Marker's own Backup button writes exactly this, CRLF and all."""
    path = tmp_path / "Netflix Watched List (2026-07-26 19_14_10).txt"
    path.write_text("70173048\r\n81208936\r\n\r\n80180071\r\n", encoding="utf-8")
    assert watched._parse(str(path)) == {"70173048", "81208936", "80180071"}


def test_a_corrupt_backup_line_is_skipped_rather_than_imported(tmp_path):
    path = tmp_path / "Netflix Watched List (x).txt"
    path.write_text("70173048\nnot-an-id\n\n81208936\n", encoding="utf-8")
    assert watched._parse(str(path)) == {"70173048", "81208936"}


def test_marking_watched_survives_the_id_being_a_number_not_a_string():
    """The rip stores nf_id as an int; the extension exports decimal strings. Comparing them
    raw would mark nothing at all."""
    records = [store.record(0, slot(title="Dark", vid=80100172)),
               store.record(1, slot(title="Arcane", vid=81435684))]
    hits = watched.apply(records, {"80100172"})

    assert hits == 1
    assert records[0]["watched"] is True and records[1]["watched"] is False
    assert "watched" in store.FLAT_COLUMNS  # so it reaches the CSV, not just the page


def test_a_record_is_not_watched_until_the_sync_says_so():
    assert store.record(0, slot())["watched"] is False


# ── anime / Korean markers ───────────────────────────────────────────────────

# These are the real genre rows Netflix returned for these titles.
NF_GENRES = {
    "70302573": ["Sci-Fi & Fantasy Anime", "Action Anime", "Japanese", "Anime Series"],
    "80217863": ["Family Time TV", "TV Action & Adventure", "TV Shows Based on Manga"],
    "80014749": ["Sitcoms", "Sci-Fi Shows", "TV Comedies", "TV Action & Adventure"],
    "81237994": ["Korean", "Teen TV Shows", "K-Dramas based on Webtoon", "TV Horror"],
    "80100172": ["German", "Sci-Fi Shows", "TV Mysteries", "Crime TV Shows"],
}


def test_netflixs_own_genre_names_decide_anime_and_korean():
    """IMDb calls Rick and Morty "Animation", so a genre-string guess would call it anime.
    Netflix's own rows say Sitcoms — and say "Korean" / "Anime Series" where it counts."""
    assert markers.classify(NF_GENRES["70302573"]) == {"anime"}
    assert markers.classify(NF_GENRES["81237994"]) == {"korean"}
    assert markers.classify(NF_GENRES["80014749"]) == set()
    assert markers.classify(NF_GENRES["80100172"]) == set()


def test_a_manga_adaptation_counts_as_anime_even_without_the_word_anime():
    """One Piece carries no "Anime" row at all — only "TV Shows Based on Manga". Requiring the
    literal word left the single most obvious anime in the gallery unmarked."""
    assert markers.classify(NF_GENRES["80217863"]) == {"anime"}


def test_applying_markers_fills_every_flag_and_keeps_the_names():
    records = [store.record(0, slot(title="One Piece", vid=80217863)),
               store.record(1, slot(title="Rick and Morty", vid=80014749))]
    counts = markers.apply(records, NF_GENRES)

    assert [r["is_anime"] for r in records] == [True, False]
    assert counts == {"anime": 1, "korean": 0}
    # The names ride along: richer than IMDb's, and a rule change then costs no refetch.
    assert records[0]["nf_genres"] == NF_GENRES["80217863"]
    for field in ("is_anime", "is_korean", "nf_genres"):
        assert field in store.FLAT_COLUMNS
    assert None not in store.flat(records[0]).values()


def test_a_title_netflix_has_no_genres_for_is_unmarked_not_crashed():
    records = [store.record(0, slot(vid=999))]
    counts = markers.apply(records, {})

    assert records[0]["is_anime"] is False and records[0]["nf_genres"] == []
    assert counts == {"anime": 0, "korean": 0}


def test_parsing_the_genre_envelope_ignores_falcors_own_keys():
    envelope = {"json": {"videos": {
        "80217863": {"$__path": ["videos", "80217863"], "genres": {
            "0": {"$__path": ["genres", "451716"], "id": 451716, "name": "Family Time TV"},
            "1": {"id": 2951909, "name": "TV Shows Based on Manga"},
            "$__path": ["videos", 80217863, "genres"],
        }},
        "$__path": ["videos"],
    }}}
    assert markers.parse_genres(envelope) == {
        "80217863": ["Family Time TV", "TV Shows Based on Manga"]}


# ── the page ─────────────────────────────────────────────────────────────────

def markup(text):
    """The page minus its HTML comments. A comment can't fetch anything, and the page's own
    prose explains what it deliberately does *not* do — which a naive text scan reads as the
    page doing it."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def test_the_page_template_reaches_for_nothing_on_the_network():
    """It has to keep working on a plane, and every record carries an https boxart URL that
    would quietly turn it into an online-only page."""
    with open(titles.PAGE, encoding="utf-8") as f:
        template = markup(f.read())

    for forbidden in ('src="http', 'href="http', "<img", "fetch(", "@import", "<iframe"):
        assert forbidden not in template, f"the page reaches out via {forbidden!r}"
    # A <link> is allowed only as the data:-URI favicon — that's bytes, not network.
    for tag in re.findall(r"<link[^>]*>", template):
        assert 'href="data:' in tag, f"non-inline <link>: {tag}"
    assert "__DATA__" in template  # the one thing Python fills in


def test_rendering_fills_the_payload_and_a_title_cannot_close_the_script_tag(tmp_path):
    dump = {
        "name": "German Dubbed Movies & TV", "url": "https://www.netflix.com/browse/audio/1/de",
        "lang": "de", "fetched_at": 1_700_000_000, "complete": True,
        "records": [store.record(0, slot(title="</script><script>alert(1)</script>"))],
    }
    dump["records"][0].update({"imdb_rating": 8.7, "imdb_votes": 1000, "weighted": 8.1,
                               "imdb_genres": ["Drama"], "match_confidence": "exact"})
    out = titles.render(titles.build(dump, titles.stats_for(dump["records"])),
                        str(tmp_path / "titles.html"))
    page = open(out, encoding="utf-8").read()

    assert "__DATA__" not in page
    # The payload must not be able to break out of its own <script> element.
    assert "</script><script>alert(1)" not in page
    assert markup(page).count('<script id="data"') == 1

    payload = page.split('type="application/json">')[1].split("</script>")[0]
    data = json.loads(payload.replace("<\\/", "</"))
    assert data["name"] == "German Dubbed Movies & TV"
    assert data["rows"][0]["title"] == "</script><script>alert(1)</script>"
    assert "raw" not in data["rows"][0]  # the dump keeps it; the page has no use for it


def rated(title, vid, genres, **kw):
    rec = store.record(0, slot(title=title, vid=vid, **kw))
    rec.update({"imdb_rating": 8.0, "imdb_votes": 5000, "weighted": 7.8, "imdb_genres": genres,
                "match_confidence": "exact"})
    return rec


def test_hidden_genres_leave_the_page_but_never_the_dump(monkeypatch):
    """"Throw out crime and documentaries" filters the *page*: the CSV and the JSON dump keep
    every title, so widening the list again is a re-render, not a re-rip."""
    monkeypatch.setattr(settings, "TITLES_HIDE_GENRES", "Crime,Documentary")
    records = [
        rated("Arcane", 1, ["Action", "Animation"]),
        rated("Better Call Saul", 2, ["Crime", "Drama"]),      # crime *drama* goes too
        rated("The Last Dance", 3, ["Biography", "Documentary"]),
        rated("Frieren", 4, ["Adventure", "Fantasy"]),
    ]
    data = titles.build({"records": records}, titles.stats_for(records))

    assert [r["title"] for r in data["rows"]] == ["Arcane", "Frieren"]
    assert data["hidden_count"] == 2
    assert "Crime" not in data["genres"]      # and the genre picker can't offer them back
    assert len(records) == 4                  # the records themselves are untouched


def test_clearing_the_hidden_genres_shows_everything(monkeypatch):
    monkeypatch.setattr(settings, "TITLES_HIDE_GENRES", "")
    records = [rated("Better Call Saul", 2, ["Crime", "Drama"])]
    data = titles.build({"records": records}, titles.stats_for(records))

    assert len(data["rows"]) == 1 and data["hidden_count"] == 0


def test_hiding_every_genre_is_a_user_error_not_an_empty_page(monkeypatch):
    monkeypatch.setattr(settings, "TITLES_HIDE_GENRES", "Drama")
    records = [rated("Better Call Saul", 2, ["Crime", "Drama"])]
    with pytest.raises(UserError, match="titles-hide-genres"):
        titles.build({"records": records}, titles.stats_for(records))


def test_the_page_needs_at_least_one_title():
    with pytest.raises(UserError):
        titles.build({"records": []}, {})
