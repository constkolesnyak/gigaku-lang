"""Convert a Language Reactor "Excel subs" export into two aligned .srt files.

Ported from polyglotka (src/polyglotka/simple_commands/excel_to_srt.py). The LR xlsx has
three columns — ``Time`` / ``Subtitle`` / ``Machine Translation`` — and this produces a
*primary* SRT from the ``Subtitle`` column (the target language) and a *secondary* SRT
from the ``Machine Translation`` column (the native-language translation), both sharing
the same computed timing so the two subtitle tracks line up.

The pure conversion logic is copied verbatim; only the file read is adapted from pandas to
openpyxl (already a gigaku dependency) so no extra dependency is needed.
"""
import glob
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass

import openpyxl

from lib.config import UserError, note, settings
from lib.subs import library
from lib.subs.language import mismatch as language_mismatch
from lib.subs.naming import sanitize, srt_base
from lib.vocab.words import remove_processed


@dataclass(frozen=True)
class SubtitleSegment:
    start_ms: int
    end_ms: int


def parse_time(value) -> int | None:
    """Parse a timestamp ('42s', '2:14', '1:02:14') into milliseconds; None if missing."""
    if value is None or (isinstance(value, float) and value != value):  # NaN check
        return None

    time_str = str(value).strip()
    if not time_str:
        return None

    if time_str.endswith("s"):
        return int(float(time_str[:-1]) * 1000)

    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = parts
            return int((int(minutes) * 60 + float(seconds)) * 1000)
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
    except ValueError as exc:
        raise ValueError(f"Invalid time format: {time_str}") from exc

    raise ValueError(f"Invalid time format: {time_str}")


def ms_to_srt(ms: float) -> str:
    """A millisecond count as an SRT timecode. Takes a float because Language Reactor's
    cue times are JavaScript numbers and arrive fractional (`begin` 3359800.0000000005),
    and the one caller that formats one is the *error* path for an incomplete export —
    where a crash in the message costs the whole run: `verify` returning "stops at …"
    died with `Unknown format code 'd' for object of type 'float'`, which is an
    uncaught ValueError, so Move to Heaven lost both its episodes to a message about
    one of them."""
    hours, remainder = divmod(int(ms), 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def estimate_end(start_ms: int, text: str, next_start_ms: int | None) -> int:
    """Estimate an end timestamp with a reading-speed heuristic, never overlapping the
    next segment."""
    MIN_DURATION_MS = 1000
    MAX_DURATION_MS = 9**9
    BASE_DURATION_MS = 400
    GAP_BETWEEN_SEGMENTS_MS = 50

    readable_chars = len(_strip_newlines(text))
    duration_ms = BASE_DURATION_MS + readable_chars * settings.LR_SUBS_MS_PER_CHAR
    duration_ms = max(MIN_DURATION_MS, min(duration_ms, MAX_DURATION_MS))

    proposed_end = start_ms + duration_ms
    if next_start_ms is None:
        return proposed_end

    latest_allowed = max(start_ms, next_start_ms - GAP_BETWEEN_SEGMENTS_MS)
    if latest_allowed <= start_ms:
        return start_ms

    return min(proposed_end, latest_allowed)


def build_segments(
    times_ms: Sequence[int | None],
    primary_texts: Sequence[str],
    secondary_texts: Sequence[str] | None = None,
) -> list[SubtitleSegment | None]:
    """Compute shared (start, end) pairs for all rows."""
    next_starts = _compute_next_starts(times_ms)
    segments: list[SubtitleSegment | None] = []

    for idx, start_ms in enumerate(times_ms):
        if start_ms is None:
            segments.append(None)
            continue

        text = _normalise_text(primary_texts[idx])
        if not text and secondary_texts is not None:
            text = _normalise_text(secondary_texts[idx])

        end_ms = estimate_end(start_ms, text, next_starts[idx])
        segments.append(SubtitleSegment(start_ms=start_ms, end_ms=end_ms))

    return segments


def create_srt_text(segments: Sequence[SubtitleSegment | None], texts: Sequence[str]) -> str:
    """Render an SRT string using the pre-computed aligned segments."""
    lines: list[str] = []
    counter = 1
    for segment, raw_text in zip(segments, texts):
        if segment is None:
            continue
        text = _normalise_text(raw_text)
        if not text:
            continue

        lines.append(str(counter))
        lines.append(f"{ms_to_srt(segment.start_ms)} --> {ms_to_srt(segment.end_ms)}")
        lines.extend(text.splitlines())
        lines.append("")
        counter += 1

    return "\n".join(lines)


def _normalise_text(value) -> str:
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return ""
    return str(value).strip()


def _strip_newlines(text: str) -> str:
    return text.replace("\n", " ").strip()


def _compute_next_starts(times_ms: Sequence[int | None]) -> list[int | None]:
    next_starts: list[int | None] = [None] * len(times_ms)
    next_start: int | None = None
    for idx in range(len(times_ms) - 1, -1, -1):
        next_starts[idx] = next_start
        current = times_ms[idx]
        if current is not None:
            next_start = current
    return next_starts


def _translation_col(header: Sequence[str]) -> int | None:
    """Index of the column the Secondary track comes from.

    An LR export can carry several translation columns — a title with official subtitles in
    your native language exports 'Human Translation' *before* 'Machine Translation'. Take the
    machine one: it's the toggle `subs` switches on, the one `_wait_translations` waits for,
    and the only one an ASR track ever has (there is no official translation of ASR text, so
    'Human Translation' is empty there — matching it first emptied the Secondary SRT and made
    `verify` report every line as untranslated). Fall back to any other translation column.
    """
    return next(
        (i for i, h in enumerate(header) if "machine translation" in h),
        next((i for i, h in enumerate(header) if "translation" in h), None),
    )


def _read_columns(xlsx_path) -> tuple[list, list[str], list[str] | None]:
    """Read Time / Subtitle / Machine-Translation columns from an LR xlsx (openpyxl)."""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    try:
        rows = list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        raise ValueError("empty xlsx")
    header = [str(c or "").strip().lower() for c in rows[0]]

    def col(match: str) -> int | None:
        return next((i for i, h in enumerate(header) if match in h), None)

    time_c, sub_c, tr_c = col("time"), col("subtitle"), _translation_col(header)
    if time_c is None or sub_c is None:
        raise ValueError(f"missing Time/Subtitle column (header={rows[0]})")

    def column(idx: int | None):
        if idx is None:
            return None
        return [(r[idx] if idx < len(r) else None) for r in rows[1:]]

    return column(time_c), column(sub_c), column(tr_c)


# How far short of the last subtitle cue an export may stop and still count as complete.
# Generous on purpose: an episode's final cue is often an isolated line over the end credits,
# long after the dialogue ends, and LR's export drops it. Measured on All of Us Are Dead E01 —
# dialogue ends at cue 825 (58:37), the last cue is 826 (1:00:08), a *91-second* gap, and every
# export stops at 58:37. A tighter limit rejected a complete episode three times and skipped it.
# Three minutes still catches the failure this guards against (an export written before the
# translations finished stops a large fraction of the episode early, not a minute).
END_TOLERANCE_MS = 180_000

# The opening words of the reason `verify` gives when the export is in the wrong language.
# A *contract*, not a message: `gigaku subs` recognises this failure class by it, because that
# one has a cure of its own (restart Language Reactor) while "stops early" and "missing a
# translation" are cured by reloading and re-exporting. Matching on a free-text sentence is
# how a caller ends up quietly not recognising it any more after a wording change.
WRONG_TRACK = "wrong track exported"


def verify(xlsx_path, expected_end_ms: int | None = None,
           require_translation: bool = True,
           language: str | None = None) -> tuple[bool, str]:
    """Is this export complete — and is it the track that was asked for? Returns (ok, reason).

    Three things are checked: the text is written in ``language``'s own script (see below), the
    export reaches the end of the episode (its last timestamp lands within ``END_TOLERANCE_MS``
    of ``expected_end_ms``, the last cue LR holds), and every subtitle line carries a
    translation (a blank *subtitle* row is a legitimate ASR gap, a blank *translation* under a
    real line is not). `gigaku subs` checks this before it deletes a downloaded xlsx; `gigaku
    srt` (convert_exports) checks it before it deletes yours.

    ``language`` is the **only check on what the file contains rather than how much of it there
    is**, and it exists because everything upstream trusts a label. On 2026-08-14 The Eminence
    in Shadow E11 and E12 exported as Japanese with every gate satisfied: LR had reverted to
    ``closedcaptions:Japanese`` in the gap between the last ``_verify_tracks`` and the export
    click, and nothing ever looked at the text. A script check settles that without a threshold
    worth arguing about — German is Latin end to end, Japanese ASR is kana end to end. It is
    skipped for a language whose script ``lib/subs/language.py`` doesn't know, so passing one
    can only ever *add* a refusal it is sure of.

    ``require_translation=False`` drops the second check, for the export `gigaku subs` now
    takes by default: with the Secondary track glossed by Claude afterwards
    (lib/subs/translate.py), LR is never asked to machine-translate and the column simply
    isn't there. The end-timestamp check is the one that mattered anyway — it is what
    distinguishes a truncated export from a complete one.

    What is deliberately NOT checked is the row *count*. LR's export merges consecutive cues
    into sentence rows — a measured 1220-cue track exports 862 complete rows — so requiring
    one row per cue rejected perfectly good exports (an 827-cue ASR track exported 703 rows
    and was thrown away as "incomplete", which then re-ripped the episode forever). Row count
    can't distinguish merging from truncation; the end timestamp can.
    """
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    except Exception as e:  # noqa: BLE001 — corrupt or half-written download
        return False, f"cannot open ({e})"
    try:
        rows = list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()

    if len(rows) < 2:
        return False, "no data rows"
    header = [str(c or "").strip().lower() for c in rows[0]]
    sub_c = next((i for i, h in enumerate(header) if "subtitle" in h), None)
    tr_c = _translation_col(header)
    if sub_c is None or (tr_c is None and require_translation):
        return False, f"missing subtitle/translation column (header={rows[0]})"

    data = rows[1:]
    # First, because "stops at 12:04" would be a misleading thing to say about a file that is
    # the wrong track entirely — and because this is the one failure a re-export can't be
    # trusted to have avoided by itself.
    if language:
        text = "\n".join(str(r[sub_c]) for r in data if sub_c < len(r) and r[sub_c])
        wrong = language_mismatch(text, language)
        if wrong:
            return False, f"{WRONG_TRACK} — {wrong}"

    if expected_end_ms:
        time_c = next((i for i, h in enumerate(header) if "time" in h), None)
        ends = [parse_time(r[time_c]) for r in data if time_c is not None and time_c < len(r)]
        last = max([ms for ms in ends if ms is not None], default=None)
        if last is None:
            return False, "no timestamps in the export"
        if last < expected_end_ms - END_TOLERANCE_MS:
            return False, (f"stops at {ms_to_srt(last)} but the subtitles run to "
                           f"{ms_to_srt(expected_end_ms)}")

    if not require_translation:
        return True, f"{len(data)} rows"

    untranslated = 0
    for row in data:
        sub = row[sub_c] if sub_c < len(row) else None
        tr = row[tr_c] if tr_c < len(row) else None
        if sub and str(sub).strip() and not (tr and str(tr).strip()):
            untranslated += 1
    if untranslated:
        return False, f"{untranslated} subtitle line(s) missing a translation"
    return True, f"{len(data)} rows, all subtitles translated"


def convert(xlsx_path, primary_srt_path, secondary_srt_path=None) -> None:
    """Write ``primary_srt_path`` (Subtitle column) and, if present, ``secondary_srt_path``
    (Machine-Translation column) from an LR xlsx, with shared aligned timing.

    ``secondary_srt_path=None`` writes the Primary alone — the shape `gigaku subs` uses now
    that the Secondary is glossed from it afterwards rather than exported beside it. The
    timing is computed identically either way, which is what lets the translator reuse it:
    it replaces the text of this file's cues and never touches a timecode.
    """
    time_col, primary_texts, secondary_texts = _read_columns(xlsx_path)
    times_ms = [parse_time(v) for v in time_col]
    segments = build_segments(times_ms, primary_texts, secondary_texts)

    with open(primary_srt_path, "w", encoding="utf-8") as f:
        f.write(create_srt_text(segments, primary_texts))
    if secondary_srt_path and secondary_texts is not None:
        with open(secondary_srt_path, "w", encoding="utf-8") as f:
            f.write(create_srt_text(segments, secondary_texts))


def convert_exports(name, season=1, start=1) -> list[str]:
    """`gigaku srt` — convert the LR xlsx exports sitting in ~/Downloads, oldest first.

    The manual counterpart to `gigaku subs`: when Language Reactor's Excel export was
    clicked by hand (or `subs` couldn't drive the browser), this turns the files into the
    same ``<Name>/<Name> - S01E03 - Primary.srt`` pairs `subs` produces, so a season is one
    directory however its subtitles were obtained.

    Episodes are numbered by file *modification time* — the order LR wrote them, which is the
    order they were exported in. ``--season 0`` names the files ``<Name> - Primary.srt``
    (a film: exactly one export, no episode number).

    Every export is converted — including one whose translations are missing or partial, which
    still yields a perfectly good Primary track. What the check decides is only whether the
    **source may be deleted**: an incomplete xlsx is converted *and kept*, because it is the
    only copy of those subtitles and re-exporting a season by hand is an hour of clicking.
    (Convert-always is what polyglotka did; delete-only-if-verified is what `gigaku subs` does.
    Doing one without the other either loses files or refuses work it could have done.)

    Output goes to SRT_TARGET_DIR/<Title>/ — your subtitle library, not the download folder.
    """
    sources = sorted(
        glob.glob(os.path.join(settings.EXPORTED_FILES_DIR, settings.LR_SUBS_GLOB)),
        key=os.path.getmtime,
    )
    if not sources:
        raise UserError(
            f'No Language Reactor subtitle exports ("{settings.LR_SUBS_GLOB}") '
            f'in "{settings.EXPORTED_FILES_DIR}".'
        )
    # Without an episode number every file resolves to the same name, so N exports would
    # overwrite one SRT pair and then all N originals would be deleted. Refuse instead.
    if not season and len(sources) > 1:
        raise UserError(
            f"--season 0 names the output after the film alone, but {len(sources)} exports are "
            f'in "{settings.EXPORTED_FILES_DIR}" — they would all overwrite each other. Move the '
            f"others away, or pass a season number."
        )

    target_root = settings.SRT_TARGET_DIR or settings.EXPORTED_FILES_DIR
    target_dir = os.path.join(target_root, sanitize(name))
    os.makedirs(target_dir, exist_ok=True)
    trash_existing(target_dir)

    written, done, kept = [], [], []
    for episode, xlsx in enumerate(sources, start):
        base = srt_base(name, season, episode)
        primary = os.path.join(target_dir, f"{base} - Primary.srt")
        secondary = os.path.join(target_dir, f"{base} - Secondary.srt")

        convert(xlsx, primary, secondary)
        written.append(primary)
        note(f'Wrote "{primary}"')

        ok, why = verify(xlsx)
        (done if ok else kept).append(xlsx)
        if not ok:
            note(f'  ↳ incomplete ({why}) — keeping "{os.path.basename(xlsx)}"')

    remove_processed(done)  # only the ones whose translations were all there
    if kept:
        note(f"{len(kept)} export(s) kept: converted, but their SRTs are missing lines.")
    # Refresh the library index so the Chrome extension finds what was just written. No
    # Netflix ids here — a hand-converted export never saw one — so these match by title.
    library.update(target_root)
    return written


def trash_existing(target_dir) -> None:
    """Move the SRT pairs already in ``target_dir`` to SRT_TRASH_DIR before a fresh run.

    Otherwise re-ripping a shorter season leaves the previous run's extra episodes sitting
    beside the new ones, and nothing tells you which rip you're watching. Off by default (no
    SRT_TRASH_DIR → the run simply overwrites what it writes)."""
    if not settings.SRT_TRASH_DIR:
        return
    stale = [
        f
        for f in os.listdir(target_dir)
        if re.fullmatch(r".+ - (Primary|Secondary)\.srt", f) or re.fullmatch(r".+_(primary|secondary)\.srt", f)
    ]
    if not stale:
        return

    trash = os.path.expanduser(settings.SRT_TRASH_DIR)
    os.makedirs(trash, exist_ok=True)
    for name in stale:
        shutil.move(os.path.join(target_dir, name), os.path.join(trash, name))
    note(f'Moved {len(stale)} existing SRT file(s) to "{trash}".')
