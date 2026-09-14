"""Scoring through the Claude Code CLI, on the subscription. Stdlib only.

The transport — how a `claude -p` call is made, and what its harness costs — lives in
`lib/claude.py`, shared with the subtitle translator. What is left here is the part that is
about *scoring*: one call per group, the reply matched back onto the cards that asked for
it, and the results handed over the moment they land.

Two of the transport's measured consequences shape this file. Groups are hundreds of cards,
because the invocation is what needs amortising rather than the rubric (`prompt.groups`).
And there is no server-side schema, so the contract is `id: score` lines and `prompt.parse`
matches on the echoed id; a group that comes back short is re-asked once by the caller.
"""
from lib import claude
from lib.anki import prompt

# Kept as names on this module: clarity calls both, and the timeout is a property of a
# `claude -p` call rather than of scoring.
TIMEOUT = claude.TIMEOUT
executable = claude.executable
describe = claude.describe


def _invoke(items, settings, system):
    """One `claude -p` call. Returns (text, usage dict, notional cost)."""
    return claude.ask(
        prompt.render(items), system, settings.CLARITY_MODEL,
        what=f"a group of {len(items)} cards",
    )


def run(groups, settings, *, system=None, on_group=None):
    """Score every group in turn. Returns ({id: score}, notional dollars).

    Sequential on purpose: the subscription is rate-limited per rolling window, and firing
    several of these at once is the reliable way to get throttled mid-run.

    `on_group` is handed each group's results the moment they land, which is what lets the
    caller persist as it goes. A long run *will* eventually meet a rate limit or a Ctrl-C,
    and everything scored before that point should survive it — so nothing here batches up
    work to hand back at the end.
    """
    scores, spent = {}, 0.0
    for index, items in enumerate(groups, 1):
        text, usage, cost = _invoke(items, settings, system or prompt.SYSTEM)
        got = prompt.parse(text, [i[0] for i in items])
        scores.update(got)
        spent += cost
        if on_group:
            on_group(index, len(groups), items, got, usage, spent)
    return scores, spent
