"""The `claude` CLI, the transport only: one invocation, its answer, and what it cost.

Shared by `gigaku clarity` (rating i+1 cards, lib/anki/score.py) and `gigaku subs`
(translating a ripped German track into Russian, lib/subs/translate.py) — one place that
knows how a Claude Code subscription is spoken to. What to ask, and how to read the answer
back, stays with each caller: exactly the split lib/telegram.py draws.

Running `claude -p` rather than the Messages API is what lets both features ride the
existing subscription instead of a separate API key — no `anthropic` dependency, and no
Batch API either (it does not exist off the Messages API, so its 50% discount was never on
the table). Three measured consequences shape every caller:

* **Every invocation carries Claude Code's own ~24k-token harness prompt.** That is
  irreducible — a bare "say OK" costs the same, with tools disabled or not. So the unit to
  amortise is the *invocation*, not the prompt, and a request wants to be hundreds of items
  rather than the couple of dozen an API call would take. Within an hour the harness is
  served from cache (`cache_creation` drops to 0 on later calls), so a long run pays once.
* **There is no server-side schema.** `output_config.format` isn't reachable from the CLI,
  so every caller's contract is plain lines it can match on an echoed id, and a reply that
  comes back short is re-asked rather than trusted.
* **It is slow and its cost is erratic.** Minutes per call, and the reply is anywhere from
  the bare answer to twenty times that when the harness decides to deliberate — measured
  across eight consecutive identical-shaped clarity calls (1,801 to ~35,000 output tokens).
  Hence per-run budgets rather than one heroic pass, and a generous timeout.
"""
import json
import shutil
import subprocess

from lib.config import UserError

# Wall time is driven by the *reply*, which grows with whatever the caller asks for: clarity
# went from ~130 to ~240 output tokens per card when its rubric grew a judgement, and a
# 200-card group went from 5 minutes to 13 — the one after it blew a 30-minute limit. An hour
# leaves room for a slow call while still failing a genuinely stuck one rather than hanging a
# run behind it.
TIMEOUT = 3600
# Claude Code reads project settings, hooks and MCP servers by default. None of that belongs
# in a headless call: it is prompt weight at best and a surprise at worst.
# What `claude --effort` accepts. Pinned here because the CLI *warns* on an unknown value and
# then silently uses the default, which is the one failure this flag exists to prevent.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

BASE_ARGS = (
    "--output-format", "json",
    "--strict-mcp-config",
    "--settings", '{"disableAllHooks":true}',
)


def executable():
    path = shutil.which("claude")
    if not path:
        raise UserError(
            "the `claude` CLI isn't on PATH — this runs through your Claude Code "
            "subscription rather than an API key."
        )
    return path


def ask(text, system, model, timeout=TIMEOUT, what="request", effort=None):
    """One `claude -p` call. Returns (reply text, usage dict, notional cost).

    ``what`` names the work in the timeout message — the only thing this can usefully say
    when a call has to be abandoned, since the fix is always "ask for less at a time".

    ``effort`` (low/medium/high/xhigh/max) is the fix for the **bimodal answer** measured on
    2026-08-07: left to the default, an identically-shaped request comes back either at
    ~20 output tokens per subtitle line or at ~215, with nothing in between, and the cheap
    mode is not merely terser — on one fixed 144-cue block it found 4 German compounds where
    the two expensive answers found 14 and 15, collapsing the rest into single Russian words.
    That made every single-run A/B a measurement of which mode was drawn rather than of the
    change under test. Naming the effort removes the draw. An unknown value is only *warned*
    about by the CLI and then ignored, so a typo here degrades to the old coin-flip rather
    than failing — hence the levels are pinned in `EFFORTS` and checked before the call.
    """
    if effort is not None and effort not in EFFORTS:
        raise UserError(f"effort must be one of {', '.join(sorted(EFFORTS))} — got {effort!r}")
    command = [
        executable(),
        "-p", text,
        "--system-prompt", system,
        "--model", model,
        *(("--effort", effort) if effort else ()),
        *BASE_ARGS,
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False,
            # Without this `claude` waits 3s for piped input it is never going to get,
            # warns, and has been seen to exit 1 — a whole batch lost to an inherited
            # stdin that nobody was writing to.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise UserError(f"`claude` didn't answer within {timeout}s for {what} — ask for less "
                        f"at a time.")
    if done.returncode != 0:
        # Both streams: `claude` puts its real complaint on stdout often enough that
        # reporting stderr alone once left a failed batch with no diagnosable cause.
        detail = (done.stderr.strip() or done.stdout.strip() or "no output")[:400]
        raise UserError(f"`claude` exited {done.returncode}: {detail}")

    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise UserError(f"couldn't parse `claude` output: {done.stdout[:400]}")
    if payload.get("is_error"):
        raise UserError(f"claude: {payload.get('result') or payload.get('subtype')}")
    return (
        payload.get("result", ""),
        payload.get("usage") or {},
        payload.get("total_cost_usd") or 0.0,
    )


def describe(usage):
    """The one line worth printing per call — mostly to show the harness is caching."""
    created = usage.get("cache_creation_input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0)
    return f"cache +{created:,}/read {read:,}, out {usage.get('output_tokens', 0):,}"
