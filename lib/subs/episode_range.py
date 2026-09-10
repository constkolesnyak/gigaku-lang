"""The `gigaku subs [N-M]` episode range — which episodes of the open season to rip.

Two things this module exists to keep straight.

**The numbers are Netflix's own ``seq``, not positions in a list.** ``seq`` is what
``naming.srt_base`` writes into ``Dark - S02E03``, so ``3-12`` means "the episodes
*labelled* E03 through E12" — the same numbers the filenames and the Netflix UI show.
Hence ``select`` filters on ``seq`` and never slices: on a season whose ``seq`` values
have a gap, ``episodes[start - 1:end]`` would quietly hand back the wrong episodes.

**Format is checked here, bounds are checked in ``select``, and that split is deliberate.**
Whether ``3--12`` is even a range needs nothing but the string, so ``parse`` runs at
dispatch — before `lib.subs.subs` (and pyobjc, and AppleScript) is imported at all, so a
typo costs nothing. Whether the season *has* an episode 12 cannot be known until
``_season_episodes()`` has talked to Chrome, so ``select`` runs there, after the episode
list and before the first rip.
"""
from dataclasses import dataclass

from lib.config import UserError

# Spelled out in every rejection: the point of the error is to teach the three forms.
FORMS = ('3-12 = episodes 3 through 12, 3- = 3 to the end of the season, '
         '-3 = episodes 1 through 3')


@dataclass(frozen=True)
class Range:
    """An inclusive episode range. An open end is ``None``, not a sentinel number — the
    season's own first/last fill it in, which is what makes ``3-`` immune to a season
    growing or to a ``seq`` that doesn't start at 1."""

    start: int | None   # None = from the season's first episode
    end: int | None     # None = to its last
    text: str           # what the user typed, so the bounds message can quote it back

    def contains(self, episode: int) -> bool:
        """Is this episode number inside the range? Open ends are unbounded.

        For filtering a set that is *already known* — the SRT library, which `gigaku
        translate` walks. `select()` cannot serve there: it validates the range against a
        season Netflix has just described and refuses one that overshoots, which is right
        when the season is authoritative and wrong when the question is only "which of the
        files on disk did they mean".
        """
        return ((self.start is None or episode >= self.start)
                and (self.end is None or episode <= self.end))


def parse(text: str | None) -> Range | None:
    """``'3-12'`` → ``Range(3, 12)``, ``'3-'`` → ``Range(3, None)``, ``'-3'`` →
    ``Range(None, 3)``. ``None`` in, ``None`` out — no range means the whole season.

    Raises ``UserError`` on anything else. The messages name the value *and* list the
    accepted forms: the range is typed by hand before an hours-long rip, so a rejection
    that doesn't teach the syntax just costs another wrong guess.
    """
    if text is None:
        return None
    text = text.strip()
    left, sep, right = text.partition("-")
    # partition, not a regex: it splits on the *first* dash, so `3--12` leaves `-12` on the
    # right and fails the isdigit check below — a regex alternation would have to spell the
    # same case out. An empty `left`/`right` is the open end; both empty is `-`, which
    # selects nothing.
    if not sep or (not left and not right) or not _digits_or_empty(left, right):
        if text.isdigit():
            # A bare number is genuinely ambiguous — "from 5 on" reads as naturally as
            # "just 5" — so it is refused rather than guessed at.
            raise UserError(
                f"episode range {text!r} is ambiguous: write {text}- for "
                f'"from {text} to the end", -{text} for "episodes 1 through {text}", '
                f"or {text}-{text} for that one episode"
            )
        raise UserError(f"the episode range must be N-M, N- or -M, not {text!r} ({FORMS})")

    start = int(left) if left else None
    end = int(right) if right else None
    if start == 0 or end == 0:
        raise UserError(f"episodes are numbered from 1, so there is no episode 0: "
                        f"{text!r} ({FORMS})")
    if start is not None and end is not None and start > end:
        raise UserError(f"the episode range {text!r} ends before it starts — "
                        f"did you mean {end}-{start}? ({FORMS})")
    return Range(start, end, text)


def _digits_or_empty(*parts: str) -> bool:
    return all(p == "" or p.isdigit() for p in parts)


def select(episodes: list[dict], rng: Range | None, what: str) -> list[dict]:
    """The episodes of ``episodes`` inside ``rng``, by ``seq``. ``what`` names the season in
    the error messages (e.g. ``"Dark season 2"``).

    Reads nothing but ``e["seq"]``, and expects them sorted (``subs._season_episodes``
    guarantees it). Raises ``UserError`` when an **explicit** bound isn't in the season —
    an omitted one is open by definition and can never be out of range, so ``3-`` is fine
    on a season of any length while ``3-12`` is not.
    """
    if rng is None:
        return episodes
    seqs = [e["seq"] for e in episodes]
    lo, hi = seqs[0], seqs[-1]
    start = lo if rng.start is None else rng.start
    end = hi if rng.end is None else rng.end

    if start < lo or end > hi:
        # The overshoot worth a hint is the common one: a real start with a made-up end
        # ("rip from 4 onwards" typed as 4-9999999999), where `4-` is what was meant.
        hint = f' — write "{start}-" for "from {start} to the end"' if start <= hi < end else ""
        raise UserError(f"{what} has episodes {lo}–{hi}, so {rng.text!r} asks for "
                        f"episodes it doesn't have{hint}")

    chosen = [e for e in episodes if start <= e["seq"] <= end]
    if not chosen:
        # Only reachable when seq has a gap and the range falls entirely inside it — rare,
        # but silently ripping nothing would look exactly like a finished run.
        have = ", ".join(str(s) for s in seqs)
        raise UserError(f"{what} has no episode between {start} and {end} (it has: {have})")
    return chosen


def spans(seqs: list[int]) -> str:
    """``[1, 2, 3, 7, 8]`` → ``'1–3, 7–8'``.

    Consecutive runs collapse, so what's being ripped stays one readable line however many
    episodes it is — and stays *exact* when the set is scattered, which a resumed season's
    leftovers usually are (the ones that failed, plus the ones never reached).

    Bare numbers, not ``E05``: the row is already labelled and the headline already named the
    season, so the prefix and the zero padding only made the one value on the line harder to
    read. (The per-episode progress lines keep ``E05`` — there it matches the filename.)
    """
    runs: list[tuple[int, int]] = []
    for s in seqs:
        if runs and s == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], s)
        else:
            runs.append((s, s))
    return ", ".join(f"{a}" if a == b else f"{a}–{b}" for a, b in runs)


def describe(todo: list[dict], chosen: list[dict], total: int,
             season: int | None, rng: Range | None) -> tuple[str, list[tuple[str, str]]]:
    """``(headline, rows)`` for the block printed before a rip starts — one fact per row,
    label and value, for the caller to align and print.

    The **Ripping** row names what will actually be *fetched* (``todo``), not what was asked
    for (``chosen``): those differ the moment a resumed season has part of the range on disk
    already, and announcing "E03–E12" and then skipping eight of them is a promise about
    work nothing is going to do.

    **Requested** appears only when it differs from Ripping. On a clean run ``3-12`` and
    "E03–E12  (10 episodes)" are the same sentence twice, and a row that restates the row
    above it is noise in the one block that has to be read at a glance.
    """
    def count(n):
        return "1 episode" if n == 1 else f"{n} episodes"

    if season is None:  # a film has one synthetic episode and no E-number in its filename
        return "film", [("Ripping", "the film" if todo else "nothing — already ripped")]

    rows = [("Ripping", f"{spans([e['seq'] for e in todo])}  ({count(len(todo))})" if todo
             else "nothing — everything asked for is already there")]
    wanted = {e["seq"] for e in todo}
    behind = [e["seq"] for e in chosen if e["seq"] not in wanted]
    if behind:
        # Only now is the ask worth quoting back: part of it is already on disk, so what was
        # typed is no longer readable off the Ripping row.
        if rng is not None:
            rows.append(("Requested", f"{rng.text}  ({count(len(chosen))})"))
        rows.append(("Already done", spans(behind)))
    return f"season {season}, {count(total)}", rows
