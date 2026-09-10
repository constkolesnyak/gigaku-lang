"""The subtitle library index the chrome/gigaku extension reads."""
import json

from lib.subs import library


def write_pair(root, show, base):
    """A complete Primary+Secondary pair, the way `subs`/`srt` write one."""
    show_dir = root / show
    show_dir.mkdir(parents=True, exist_ok=True)
    (show_dir / f"{base} - Primary.srt").write_text("1\n", encoding="utf-8")
    (show_dir / f"{base} - Secondary.srt").write_text("1\n", encoding="utf-8")


def test_parse_base_reads_season_and_episode():
    assert library.parse_base("Dark - S02E03") == ("Dark", 2, 3)
    # A film has no numbering: srt_base() names it after the title alone.
    assert library.parse_base("Arrival") == ("Arrival", None, 1)
    # A title that itself contains " - " must not be eaten by the episode suffix.
    assert library.parse_base("Kill Bill - Vol. 2 - S01E07") == ("Kill Bill - Vol. 2", 1, 7)


def test_scan_indexes_complete_pairs_only(tmp_path):
    write_pair(tmp_path, "Dark", "Dark - S02E03")
    (tmp_path / "Dark" / "Dark - S02E04 - Primary.srt").write_text("1\n", encoding="utf-8")
    (tmp_path / "Dark" / "notes.txt").write_text("x\n", encoding="utf-8")

    entries = library.scan(str(tmp_path))

    assert list(entries) == ["dark|2|3"]  # E04 has no Secondary — not a loadable pair
    assert entries["dark|2|3"] == {
        "dir": "Dark",
        "primary": "Dark - S02E03 - Primary.srt",
        "secondary": "Dark - S02E03 - Secondary.srt",
        "title": "Dark",
        "season": 2,
        "episode": 3,
    }


def test_title_key_ignores_case_and_illegal_characters():
    # The extension sanitizes the Netflix title the same way naming.sanitize() does, so a
    # title with a colon still finds the directory it was written to.
    assert library.title_key("Money Heist: Berlin", 1, 2) == library.title_key(
        "money heist  berlin", 1, 2
    )


def test_update_records_netflix_ids_and_keeps_earlier_ones(tmp_path):
    write_pair(tmp_path, "Dark", "Dark - S02E03")
    write_pair(tmp_path, "Dark", "Dark - S02E04")

    library.update(str(tmp_path), ids={81237996: ("Dark", 2, 3)})
    library.update(str(tmp_path), ids={81237997: ("Dark", 2, 4)})

    index = json.loads((tmp_path / library.INDEX_NAME).read_text(encoding="utf-8"))
    # The id learned in the first run survives the second run's rescan.
    assert index["byId"] == {"81237996": "dark|2|3", "81237997": "dark|2|4"}
    assert index["byTitle"]["dark|2|3"]["primary"] == "Dark - S02E03 - Primary.srt"


def test_update_drops_ids_whose_episode_left_the_library(tmp_path):
    write_pair(tmp_path, "Dark", "Dark - S02E03")
    library.update(str(tmp_path), ids={81237996: ("Dark", 2, 3)})

    for srt in (tmp_path / "Dark").iterdir():
        srt.unlink()
    library.update(str(tmp_path))

    index = json.loads((tmp_path / library.INDEX_NAME).read_text(encoding="utf-8"))
    assert index == {"byId": {}, "byTitle": {}}  # a stale id would point at nothing


def test_update_ignores_ids_with_no_files_on_disk(tmp_path):
    write_pair(tmp_path, "Dark", "Dark - S02E03")

    library.update(str(tmp_path), ids={999: ("Dark", 9, 9)})

    index = json.loads((tmp_path / library.INDEX_NAME).read_text(encoding="utf-8"))
    assert index["byId"] == {}


def test_update_survives_a_corrupt_index(tmp_path):
    write_pair(tmp_path, "Dark", "Dark - S02E03")
    (tmp_path / library.INDEX_NAME).write_text("{not json", encoding="utf-8")

    library.update(str(tmp_path))

    index = json.loads((tmp_path / library.INDEX_NAME).read_text(encoding="utf-8"))
    assert list(index["byTitle"]) == ["dark|2|3"]
