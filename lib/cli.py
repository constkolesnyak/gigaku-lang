"""CLI entry point for the `gigaku` command.

    gigaku sound                     mouse-wheel → Apple TV volume daemon
    gigaku subs [N-M]                rip a Netflix season's subtitles via Language Reactor
                                     (3-12 / 3- / -3 narrow it; default: the whole season)
    gigaku srt NAME                  convert LR Excel subs already in ~/Downloads to SRT

    gigaku titles [URL]              rip a Netflix browse gallery, rank it by IMDb rating

    gigaku import                    pull words from Language Reactor + Migaku into the cache
    gigaku plots                     draw the vocabulary history (HTML, opens in a browser)
    gigaku publish                   push that page to the Pi, served over Tailscale
    gigaku report --send             the week's progress to Telegram, one chart per language
    gigaku backup                    snapshot the word cache + Migaku + AnkiMorphs to git
    gigaku kanji                     TSV: kanji by how many words you know with them
    gigaku anki                      an Anki query for the kanji worth studying
    gigaku clarity --loop            rate i+1 cards by how obvious the word is
    gigaku words --lang de           print words
    gigaku info                      settings, cache, where things live
    gigaku clear-cache               forget the word cache

Every field of lib/config.Settings can be set by a GIGAKU_<NAME> environment variable and
overridden again by the matching --flag; flags win. `gigaku info` prints the lot.
"""
import argparse
import json
import os
import sys

from lib.config import UserError, settings

# Commands that read exports and the word cache — they all take the same settings flags.
WORD_COMMANDS = ("import", "plots", "publish", "backup", "report", "kanji", "anki", "words", "srt")


def _parser():
    parser = argparse.ArgumentParser(
        prog="gigaku",
        description="Apple TV volume by mouse wheel, Netflix subtitle ripping, and "
        "vocabulary tracking for Language Reactor + Migaku.",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, help):
        return commands.add_parser(name, help=help, description=help)

    add("sound", "Run the mouse-wheel → Apple TV volume daemon.")

    p = add("subs", "Rip the current Netflix season's subtitles via Language Reactor.")
    # `gigaku subs -3` reaches us as a positional only because no option string of this
    # subparser looks like a negative number (argparse's _negative_number_matcher, the
    # prefix `-\.?\d` since 3.13). Adding a `-N`-shaped flag here would silently turn it
    # into "unrecognized arguments" — tests/test_cli.py pins that it doesn't.
    # All three default to None: _subs_args fills the defaults in, after the shift.
    p.add_argument("episodes", nargs="?", metavar="N-M", default=None,
                   help="episodes to rip: 3-12, 3- (to the end), -3 (from the start). "
                        "Default: the whole season.")
    p.add_argument("audio", nargs="?", default=None, help="audio track (default: German)")
    p.add_argument("subtitle", nargs="?", default=None,
                   help='subtitle track (default: "ASR Pro German")')
    # Not `-N`-shaped, so `gigaku subs -3` still parses (see the note above).
    p.add_argument("--no-translate", dest="subs_translate", action="store_false", default=None,
                   help="don't gloss the Secondary track with Claude — ask Language Reactor "
                        "for its machine translation instead, as the rip used to")
    # Not `-N`-shaped either, for the same reason the comment above gives.
    p.add_argument("--no-spell", dest="subs_spell", action="store_false", default=None,
                   help="don't proofread the ripped German before glossing it "
                        "(--no-translate already implies this — the gloss is where it runs)")

    p = add("translate", "Gloss ripped German subtitles into Russian with Claude.")
    p.add_argument("name", nargs="?", default=None,
                   help="only episodes whose show directory contains this (default: all)")
    p.add_argument("--season", type=int, default=None, help="only this season")
    p.add_argument("--episodes", metavar="N-M", default=None,
                   help="only these episode numbers: 3-12, 3-, -3")
    p.add_argument("--redo", action="store_true",
                   help="re-gloss episodes that already have a Secondary track")
    p.add_argument("--dry-run", action="store_true", help="list what would be glossed, and stop")
    p.add_argument("--srt-target-dir", metavar="DIR",
                   help="the subtitle library to walk (default: the extension's subs/ folder)")
    # Both defaults are read from Settings rather than typed out: the group one said 200 for a
    # while after the default became 50, and a help line that lies is worse than none.
    p.add_argument("--subs-translate-model", metavar="NAME",
                   help=f"claude model (default: {settings.SUBS_TRANSLATE_MODEL})")
    p.add_argument("--subs-translate-group", type=int, metavar="N",
                   help=f"subtitle lines per request (default: {settings.SUBS_TRANSLATE_GROUP})")

    p = add("spell", "Proofread ripped German subtitles with Claude — ASR spelling only.")
    p.add_argument("name", nargs="?", default=None,
                   help="only episodes whose show directory contains this (default: all)")
    p.add_argument("--season", type=int, default=None, help="only this season")
    p.add_argument("--episodes", metavar="N-M", default=None,
                   help="only these episode numbers: 3-12, 3-, -3")
    p.add_argument("--redo", action="store_true",
                   help="proofread again what this rubric has already been over")
    p.add_argument("--dry-run", action="store_true",
                   help="what the German dictionary doubts, per episode — no model call, "
                        "no cost")
    p.add_argument("--show", action="store_true",
                   help="print every correction already made, and change nothing")
    p.add_argument("--revert", action="store_true",
                   help="restore the German exactly as it was ripped, from the cached copy")
    p.add_argument("--srt-target-dir", metavar="DIR",
                   help="the subtitle library to walk (default: the extension's subs/ folder)")
    p.add_argument("--subs-spell-model", metavar="NAME",
                   help=f"claude model (default: {settings.SUBS_SPELL_MODEL})")
    p.add_argument("--subs-spell-group", type=int, metavar="N",
                   help=f"subtitle lines per request (default: {settings.SUBS_SPELL_GROUP})")

    p = add("srt", "Convert Language Reactor Excel subs already in ~/Downloads into SRT pairs.")
    p.add_argument("name", help="show or film title — names the output directory and the files")
    p.add_argument("--season", type=int, default=1, help="season number, 0 for a film (default: 1)")
    p.add_argument("--start", type=int, default=1,
                   help="episode number of the oldest export (default: 1)")
    p.add_argument("--srt-target-dir", metavar="DIR",
                   help="where the SRT pairs go (default: next to the exports)")
    p.add_argument("--srt-trash-dir", metavar="DIR",
                   help="move SRTs already in the target directory here first")

    p = add("titles", "Rip a Netflix browse gallery and rank it by IMDb rating.")
    p.add_argument("url", nargs="?", help="gallery URL (default: TITLES_URL)")
    p.add_argument("--probe", action="store_true",
                   help="print what the page offers and exit — use when a Netflix build "
                        "change breaks the rip")
    p.add_argument("--refresh", action="store_true", help="re-rip instead of reusing the dump")
    p.add_argument("--refresh-imdb", action="store_true",
                   help="re-download the IMDb datasets even if the cached ones are fresh")
    p.add_argument("--limit", type=int, metavar="N", help="stop after N titles (for a smoke test)")
    p.add_argument("--no-imdb", dest="imdb", action="store_false",
                   help="skip the IMDb join — just rip the list")
    p.add_argument("--no-watched", dest="watched", action="store_false",
                   help="don't re-read the Netflix Watched Marker list from Chrome")
    p.add_argument("--no-mark", dest="mark", action="store_false",
                   help="skip the anime/Korean markers (they cost a gallery rip each, once)")
    p.add_argument("--titles-mark", metavar="anime,korean",
                   help="which markers to attach ('' for none)")
    p.add_argument("--titles-sublists", metavar="su,az,za,yr",
                   help="which of the genre node's lists to walk and merge (the storefront "
                        "only shows 'su')")
    p.add_argument("--titles-extra-ids", metavar="ID,URL",
                   help="Netflix ids or /title/ URLs to pull in by hand — for titles the "
                        "gallery leaves out; they show up tagged NEW")
    p.add_argument("--tsv", action="store_true",
                   help="print the flat table to stdout instead of opening the page")
    p.add_argument("--no-open", dest="open", action="store_false",
                   help="write the page but don't open a browser")
    p.add_argument("--titles-out", metavar="PATH", help="where to write the HTML")
    p.add_argument("--titles-csv", metavar="PATH", help="where to write the CSV")
    p.add_argument("--titles-hide-genres", metavar="Crime,Documentary",
                   help="genres to leave out of the page ('' shows everything; the CSV and "
                        "the dump always keep them)")
    p.add_argument("--imdb-bayes", metavar="M,C",
                   help="min votes,prior for the weighted score (default: 2000,7.0)")

    p = add("import", "Import words from Language Reactor / Migaku exports and from Chrome.")
    p.add_argument("--no-chrome", dest="chrome", action="store_false", default=None,
                   help="don't read Migaku's word list out of Chrome's IndexedDB")
    p.add_argument("--known-morphs-save-langs", metavar="ja,de",
                   help="also write AnkiMorphs known-morphs CSVs for these languages")

    p = add("plots", "Draw the vocabulary history as an HTML page and open it.")
    p.add_argument("--plots-title", metavar="TITLE", help="page title")
    p.add_argument("--plots-out", metavar="PATH", help="where to write the HTML")
    p.add_argument("--plots-hidden-langs", metavar="en,fr",
                   help="languages whose lines start toggled off (default: en)")
    p.add_argument("--no-open", dest="open", action="store_false",
                   help="write the page but don't open a browser")

    p = add("publish", "Render the page and rsync it into PUBLISH_DIR, locally or over ssh.")
    p.add_argument("--publish-host", metavar="HOST",
                   help="ssh host to push to (default: empty — a local copy into PUBLISH_DIR)")
    p.add_argument("--publish-dir", metavar="DIR", help="directory to fill")

    p = add("report", "Send the week's progress to Telegram: one chart card per language.")
    p.add_argument("--send", dest="post", action="store_true",
                   help="post it (else a dry run: write the PNGs, print the message)")
    p.add_argument("--from-backup", action="store_true",
                   help="read vocab-backup's words.json instead of the live cache")
    p.add_argument("--report-langs", metavar="de,ja",
                   help="languages to card (default: everything PLOTS_HIDDEN_LANGS keeps)")
    p.add_argument("--weeks", dest="report_weeks", type=int, metavar="N",
                   help="weeks of bars on each card (default: 12)")
    p.add_argument("--report-dir", metavar="DIR", help="where the cards are written")
    p.add_argument("--report-site-dir", metavar="DIR",
                   help="where the weekly site pages are written (publish copies them)")

    p = add("backup", "Snapshot the word cache + Migaku + AnkiMorphs into a git repo and push it.")
    p.add_argument("--backup-dir", metavar="DIR", help="the git repo to snapshot into")
    p.add_argument("--backup-remote", metavar="REMOTE",
                   help="git remote to push to (empty to commit locally, don't push)")
    p.add_argument("--backup-stale-days", type=int, metavar="N",
                   help="alert when Migaku's data hasn't moved in this many days")
    p.add_argument("--force", action="store_true",
                   help="commit even if a dataset shrank past the safety threshold")

    add("kanji", "Print a TSV of kanji ranked by how many words you know with them.")

    p = add("anki", "Print an Anki search query for the kanji worth unsuspending.")
    p.add_argument("--anki-min-counts", metavar="K,L",
                   help="minimum known,learning word counts for a kanji (e.g. 7,9)")
    p.add_argument("--anki-filters", metavar="QUERY", help='e.g. "deck:漢字 is:suspended"')
    p.add_argument("--anki-kanji-field", metavar="FIELD", help="the Anki field holding the kanji")

    p = add("clarity", "Rate i+1 alternates by how obvious the unknown word is from its "
                       "sentence (fills My Clarity, which feeds My Sort Key).")
    p.add_argument("--lang", default="ja", metavar="LANG",
                   help="which language's queue to score: ja (default) or de — each has its "
                        "own named rubric and score cache")
    p.add_argument("--calibrate", action="store_true",
                   help="sample the collection and print what the morph threshold should "
                        "be, whether the scores add anything over am-all-morphs-count, "
                        "and whether they repeat")
    p.add_argument("--add-field", action="store_true",
                   help="create the note field once (bumps the schema — Anki will ask "
                        "for a one-way sync)")
    p.add_argument("--reset", action="store_true",
                   help="clear every My Clarity value and the cache, and start over — "
                        "for when the rubric changed and old scores can't be compared "
                        "with new ones")
    p.add_argument("--restale", action="store_true",
                   help="re-score the words an older rubric judged, whole words at a "
                        "time, under the normal --words budget — the graded alternative "
                        "to --reset, which throws every score away")
    p.add_argument("--words", dest="clarity_words", type=int, metavar="N",
                   help="words to score this run, in study order (0 = all)")
    p.add_argument("--per-word", dest="clarity_per_word", type=int, metavar="K",
                   help="cap alternates judged per word (default 0 = judge them all)")
    p.add_argument("--min-morphs-de", dest="clarity_min_morphs_de", type=int, metavar="N",
                   help="the German floor (default: 5 — measured apart from ja's)")
    p.add_argument("--min-morphs", dest="clarity_min_morphs", type=int, metavar="N",
                   help="drop sentences with fewer morphs than this — they carry no "
                        "context (default: 8)")
    p.add_argument("--clarity-model", metavar="MODEL",
                   help="model for `claude -p` (default: sonnet)")
    p.add_argument("--group", dest="clarity_group", type=int, metavar="N",
                   help="cards per `claude` call (default: 200)")
    p.add_argument("--loop", action="store_true",
                   help="keep taking --words budgets until nothing is left to score, "
                        "backing off and retrying when a pass fails")
    p.add_argument("--dry-run", action="store_true",
                   help="print how long it would take and stop")
    p.add_argument("--yes", action="store_true", help="don't ask before starting")

    p = add("words", "Print the words of one language.")
    p.add_argument("--lang", required=True, metavar="de", help="language code")
    p.add_argument("--stage", default="", choices=["", "known", "learning"],
                   help="default: both known and learning")

    add("info", "Print the settings, the cache, and where everything lives.")
    add("clear-cache", "Delete the word cache.")

    for name in WORD_COMMANDS:
        p = commands.choices[name]
        p.add_argument("--exported-files-dir", metavar="DIR",
                       help="where LR/Migaku exports land (default: ~/Downloads)")
        p.add_argument("--no-rm", dest="rm_processed_files", action="store_false", default=None,
                       help="keep the export files instead of deleting them once imported")
    return parser


# Commands whose flags configure Settings. The word commands additionally get validate()'d
# against the export directories they read; `titles` touches none of those, so a stray
# GIGAKU_KNOWN_MORPHS_DIR must not take it out (same reasoning as `sound` and `subs`).
SETTINGS_COMMANDS = WORD_COMMANDS + ("titles", "clarity", "subs", "translate", "spell")

# What the command *does* vs. what merely configures it: everything not listed here is a
# Settings field and goes through settings.override().
ACTION_ARGS = frozenset({"command", "audio", "subtitle", "episodes", "name", "season",
                         "start", "lang", "stage", "open", "force", "url", "probe",
                         "refresh", "refresh_imdb", "limit", "imdb", "watched", "mark", "tsv",
                         "post", "from_backup",
                         "calibrate", "add_field", "reset", "restale", "dry_run", "yes",
                         "loop", "redo", "show", "revert"})


def _subs_args(args):
    """``(range, audio, subtitle)`` out of `subs`'s three optional positionals.

    argparse fills optional positionals strictly left to right and does **not** disambiguate
    them by shape — measured: ``gigaku subs German "ASR Pro German"`` (the form README
    documents) lands the track name in ``episodes``. Shape decides here instead: a range
    starts with a digit or a ``-``, a track name never does, so an unrange-shaped first
    token shifts down into audio/subtitle and both forms keep working.
    """
    from lib.subs import episode_range  # pure stdlib — no pyobjc on this path

    given = [v for v in (args.episodes, args.audio, args.subtitle) if v is not None]
    ranged = given and (given[0][:1].isdigit() or given[0].startswith("-"))
    text = given.pop(0) if ranged else None
    if len(given) > 2:
        raise UserError(f"unexpected argument {given[2]!r} — "
                        "usage: gigaku subs [N-M] [AUDIO] [SUBTITLE]")
    audio = given[0] if given else "German"
    subtitle = given[1] if len(given) > 1 else "ASR Pro German"
    return episode_range.parse(text), audio, subtitle


def main():
    parser = _parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        raise SystemExit(1)

    try:
        # Only the word commands read these settings — and only they should be gated by them.
        # `sound` and `subs` touch neither ~/Downloads nor ANKI_MIN_COUNTS, so a stray
        # GIGAKU_KNOWN_MORPHS_DIR in a shell profile must not take out the volume daemon.
        if args.command in SETTINGS_COMMANDS:
            settings.override(
                validate=args.command in WORD_COMMANDS,
                **{k: v for k, v in vars(args).items() if k not in ACTION_ARGS},
            )
        _run(args)
    except UserError as exc:
        print(f"gigaku: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)


def _run(args):
    match args.command:
        case "sound":
            # The daemon is a script in scripts/, not a package module: load it by path.
            import importlib.util

            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "scroll_volume.py")
            spec = importlib.util.spec_from_file_location("scroll_volume", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.main()

        case "subs":
            # Parsed before the import: a malformed range is answered without dragging
            # pyobjc/AppleScript in, and before anything touches Chrome.
            rng, audio, subtitle = _subs_args(args)
            from lib.subs import subs

            try:
                subs.run(audio, subtitle, episodes=rng)
            except subs.ExportError as exc:
                raise UserError(f"subs failed: {exc}")
            except subs._Aborted as exc:
                raise UserError(f"subs stopped: {exc.reason} — fix that and re-run; "
                                "it resumes where it stopped.")

        case "translate":
            # Same split as `subs`: the range's *shape* is answered here, by the pure module,
            # before the translator (and `claude`) is reached for at all.
            from lib.subs import episode_range

            rng = episode_range.parse(args.episodes)
            from lib.subs import translate

            translate.main(name=args.name, season=args.season, episodes=rng,
                           redo=args.redo, dry_run=args.dry_run)

        case "spell":
            # Same split as `subs` and `translate`: the range's *shape* is answered by the
            # pure module, before AppKit or `claude` is reached for at all.
            from lib.subs import episode_range

            rng = episode_range.parse(args.episodes)
            from lib.subs import spell

            spell.main(name=args.name, season=args.season, episodes=rng, redo=args.redo,
                       dry_run=args.dry_run, revert_files=args.revert, show=args.show)

        case "srt":
            from lib.subs import excel_to_srt

            excel_to_srt.convert_exports(args.name, season=args.season, start=args.start)

        case "titles":
            if args.probe:
                from lib.netflix import gallery

                print(json.dumps(gallery.probe(args.url), indent=2, ensure_ascii=False))
                return

            from lib.netflix import titles

            titles.main(url=args.url, refresh=args.refresh, refresh_imdb=args.refresh_imdb,
                        limit=args.limit, imdb=args.imdb, watched=args.watched,
                        mark=args.mark, as_tsv=args.tsv, open_browser=args.open)

        case "import":
            from lib.vocab.words import import_words

            # The only command allowed to delete the exports it just read.
            import_words(cache_allowed=False, consume=True)

        case "plots":
            from lib.vocab import plots

            plots.main(open_browser=args.open)

        case "publish":
            from lib.vocab import publish

            publish.main()

        case "report":
            from lib.vocab import report

            report.main(post=args.post, from_backup=args.from_backup)

        case "backup":
            from lib.vocab import backup

            backup.main(force=args.force)

        case "kanji" | "anki":
            from lib.vocab import kanji
            from lib.vocab.words import import_words

            found = kanji.collect(import_words())
            print(kanji.anki_query(found) if args.command == "anki" else kanji.tsv(found))

        case "clarity":
            from lib.anki import clarity

            clarity.set_lang(args.lang)
            if args.add_field:
                clarity.add_field(assume_yes=args.yes)
                return
            if args.reset:
                clarity.reset(assume_yes=args.yes)
                return
            if args.calibrate:
                clarity.calibrate(assume_yes=args.yes, dry_run=args.dry_run)
            else:
                clarity.main(assume_yes=args.yes, dry_run=args.dry_run, loop=args.loop,
                             restale=args.restale)

        case "words":
            from lib.vocab.words import import_words, word_list

            print("\n".join(word_list(import_words(), args.lang, args.stage)))

        case "info":
            _info()

        case "clear-cache":
            from lib.vocab.words import clear_cache

            clear_cache()


def _info():
    from dataclasses import fields

    from lib import config
    from lib.vocab.words import Stage, languages, read_cache

    words = read_cache()
    print(f"word cache   {config.WORDS_CACHE}  ({len(words):,} words)")
    for lang in languages(words):
        counts = {
            stage: sum(1 for w in words if w.language == lang and w.stage == stage)
            for stage in (Stage.KNOWN, Stage.LEARNING)
        }
        print(f"  {lang}  {counts[Stage.KNOWN]:,} known · {counts[Stage.LEARNING]:,} learning")

    print("\nsettings — GIGAKU_<NAME> to change, or the matching --flag:")
    for f in sorted(fields(settings), key=lambda f: f.name):
        print(f"  {f.name:<24} {getattr(settings, f.name)!r}")
