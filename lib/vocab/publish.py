"""`gigaku publish` — render the vocabulary page and hand it to the web server.

A small static server serves `PUBLISH_DIR` at `PUBLISH_URL` — bound to
the Mac's tailnet address, so the page is one URL away from any of your devices but
visible only inside the tailnet, which matters: the kanji table lists the actual Japanese
words you know. (The Pi and its `tailscale serve` are retired; PUBLISH_HOST="" means the
rsync target is a local directory and no ssh is involved. Set PUBLISH_HOST to any ssh
host and the old push-over-ssh shape comes back unchanged.)

**No server could fetch any of this itself, and neither can the page.** The word cache
lives on this Mac, and Migaku's word list lives in *Chrome's* IndexedDB — which no web
page may read, whatever domain serves it, because IndexedDB is scoped to its origin and
pages get no filesystem. So the direction is fixed: the Mac gathers, renders, and pushes;
the server only serves files.
"""
import os
import shutil
import subprocess
import tempfile

from lib.vocab import plots
from lib.config import UserError, note, settings


def rsync_command(source_dir, host, target_dir):
    """The exact command `publish` runs — kept separate so a test can read it without a network.

    The trailing slash on the source is load-bearing: without it rsync copies the *directory*
    into the target, giving you gigaku-site/site/index.html. `--delete` keeps the target from
    accumulating files this Mac no longer renders — which also means the staging dir must
    assemble *everything* published. An empty host makes the target a local directory (the
    served folder); rsync handles both spellings identically."""
    target = f"{host}:{target_dir}/" if host else f"{os.path.expanduser(target_dir).rstrip('/')}/"
    return [
        "rsync",
        "-az",
        "--delete",
        f"{source_dir.rstrip('/')}/",
        target,
    ]


def main():
    from lib.vocab.words import import_words

    if not shutil.which("rsync"):
        raise UserError("rsync is not installed on this Mac.")

    data = plots.build(import_words())

    with tempfile.TemporaryDirectory() as staging:
        # index.html, so the URL is the bare directory rather than …/plots.html. The
        # rsync below runs --delete, so staging must assemble *everything* published.
        report_site = os.path.expanduser(settings.REPORT_SITE_DIR)
        has_report = os.path.isdir(report_site) and os.listdir(report_site)
        nav = '<a class="ghost" href="report/">Weekly report</a>' if has_report else ""
        plots.render(data, os.path.join(staging, "index.html"), nav=nav)
        if has_report:
            shutil.copytree(report_site, os.path.join(staging, "report"))
        else:
            # Say it rather than silently shipping a site with no report section — this is
            # what a wiped cache looks like, and `gigaku report` is the one-command fix.
            note("No weekly report pages yet — publishing the chart page alone.")

        host, target_dir = settings.PUBLISH_HOST, settings.PUBLISH_DIR
        where = f"{host}:{target_dir}" if host else target_dir
        command = rsync_command(staging, host, target_dir)
        note(f"Publishing to {where} …")
        result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode:
        hint = (f"Check that `ssh {host}` works and {target_dir} exists." if host
                else f"Check that {target_dir} exists.")
        raise UserError(
            f"Could not publish to {where}: {result.stderr.strip() or 'rsync failed'}\n{hint}"
        )

    kanji = len(data["kanji"])
    note(
        f'Published {data["total"]:,} words'
        + (f" and {kanji:,} kanji" if kanji else "")
        + f" → {settings.PUBLISH_URL}"
    )
