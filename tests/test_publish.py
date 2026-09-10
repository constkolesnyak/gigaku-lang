"""`gigaku publish` — the deploy. rsync is stubbed; the command isn't."""
from datetime import datetime

import pytest

from lib.vocab import publish
from lib.config import UserError, settings
from lib.vocab.words import Stage, Word


@pytest.fixture
def one_word():
    return [Word("Haus", "de", Stage.KNOWN, datetime(2026, 1, 1))]


def test_rsync_command_mirrors_the_directory_contents(tmp_path):
    command = publish.rsync_command(str(tmp_path), "pi", "/home/me/gigaku-site")

    assert command[0] == "rsync"
    assert "--delete" in command  # the remote must not accumulate stale files
    # The trailing slash is the difference between filling the target and nesting inside it.
    assert command[-2] == f"{tmp_path}/"
    assert command[-1] == "pi:/home/me/gigaku-site/"


def test_a_source_path_with_a_trailing_slash_does_not_double_it(tmp_path):
    command = publish.rsync_command(f"{tmp_path}/", "pi", "/srv/site")
    assert command[-2] == f"{tmp_path}/"


def test_an_empty_host_targets_a_local_directory(tmp_path):
    """The local shape: no ssh anywhere, the target is the served directory itself."""
    command = publish.rsync_command(str(tmp_path), "", "~/Sites/gigaku")
    assert ":" not in command[-1]
    assert command[-1].endswith("/Sites/gigaku/")
    assert command[-1].startswith("/")  # ~ expanded — rsync must not guess


def test_publish_renders_an_index_and_pushes_it(monkeypatch, one_word, capsys):
    monkeypatch.setattr(settings, "PUBLISH_HOST", "pi")
    monkeypatch.setattr(settings, "PUBLISH_DIR", "/home/me/gigaku-site")
    monkeypatch.setattr("lib.vocab.words.import_words", lambda *a, **k: one_word)

    pushed = {}

    def fake_rsync(command, capture_output, text):
        # The page must exist, be named index.html (so the URL is the bare directory), and
        # still be self-contained by the time it is handed to rsync.
        source = command[-2]
        with open(source + "index.html", encoding="utf-8") as f:
            pushed["html"] = f.read()
        pushed["command"] = command
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})

    monkeypatch.setattr("subprocess.run", fake_rsync)
    publish.main()

    assert "Haus" not in pushed["html"]  # the page carries counts, not the words themselves
    assert "<title>" in pushed["html"] and 'src="http' not in pushed["html"]
    assert pushed["command"][-1] == "pi:/home/me/gigaku-site/"
    assert settings.PUBLISH_URL in capsys.readouterr().err  # tells you where it went


def test_a_failing_rsync_is_a_user_error_not_a_traceback(monkeypatch, one_word):
    monkeypatch.setattr("lib.vocab.words.import_words", lambda *a, **k: one_word)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 255, "stderr": "ssh: no route to host"}),
    )

    with pytest.raises(UserError, match="no route to host"):
        publish.main()


def test_publish_carries_the_report_pages_and_links_to_them(monkeypatch, one_word, tmp_path):
    """--delete means staging must assemble everything published; the report dir rides
    along whole, and the chart page gains the cross-link only when there is a target."""
    site = tmp_path / "report-site"
    site.mkdir()
    (site / "index.html").write_text("the week", encoding="utf-8")
    monkeypatch.setattr(settings, "REPORT_SITE_DIR", str(site))
    monkeypatch.setattr("lib.vocab.words.import_words", lambda *a, **k: one_word)

    staged = {}

    def fake_rsync(command, capture_output, text):
        source = command[-2]
        staged["index"] = open(source + "index.html", encoding="utf-8").read()
        staged["report"] = open(source + "report/index.html", encoding="utf-8").read()
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})

    monkeypatch.setattr("subprocess.run", fake_rsync)
    publish.main()

    assert staged["report"] == "the week"
    assert 'href="report/"' in staged["index"]


def test_an_empty_report_dir_publishes_the_chart_alone_and_says_so(monkeypatch, one_word,
                                                                   tmp_path, capsys):
    monkeypatch.setattr(settings, "REPORT_SITE_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr("lib.vocab.words.import_words", lambda *a, **k: one_word)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""}),
    )

    publish.main()

    err = capsys.readouterr().err
    assert "No weekly report pages yet" in err
