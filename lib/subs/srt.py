"""Read and write .srt files. Pure stdlib, pure logic — no browser, no config.

Nothing in gigaku read an SRT before this: `excel_to_srt` only ever *wrote* one, straight
out of the LR export. The translator needs the other direction — the German Primary track
comes back off disk, its text is replaced, and the Secondary is written beside it.

Cutting an episode into request-sized windows (`groups`/`regroup`) lives here too, beside
`Cue`. It used to sit in `translate_prompt.py`, which was fine while the glosser was the only
pass over a Primary; it is about slicing cues, not about any rubric, and `lib/subs/spell.py`
needs exactly the same windows.

**A cue's timecode line is carried through verbatim, never re-derived.** It is a string
here and stays a string: parsing it into milliseconds and formatting it back would be exact
today and is a silent way to lose a millisecond the day a file arrives with a different
shape. The whole point of translating an existing Primary is that only the text changes.
"""
import os
import tempfile
from dataclasses import dataclass

# The one thing that identifies a timing line, and so a cue.
ARROW = " --> "


@dataclass(frozen=True)
class Cue:
    index: int      # the cue's own number, as written in the file — the id the model echoes
    timecode: str   # "00:00:28,000 --> 00:00:31,920", verbatim
    text: str       # may hold newlines, though a ripped Primary never does


def parse(content: str) -> list[Cue]:
    """Blocks of ``index / timecode / text`` → cues. Anything malformed is skipped."""
    cues: list[Cue] = []
    for block in content.replace("\r\n", "\n").strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) < 2 or ARROW not in lines[1]:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue
        cues.append(Cue(index, lines[1].strip(), "\n".join(lines[2:]).strip()))
    return cues


def render(cues) -> str:
    """Cues → the file body, in the shape `excel_to_srt.create_srt_text` already writes."""
    out = []
    for cue in cues:
        out += [str(cue.index), cue.timecode, *cue.text.split("\n"), ""]
    return "\n".join(out)


def read(path) -> list[Cue]:
    # utf-8-sig: a BOM would otherwise land inside the first cue's index and drop it.
    with open(path, encoding="utf-8-sig") as handle:
        return parse(handle.read())


def write(path, cues) -> None:
    """Write atomically — a half-written Secondary is one the extension would load."""
    path = str(path)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".srt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render(cues))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def groups(cues, size, context):
    """Split an episode into requests: [[(cue, needed), …], …].

    Each request holds ``size`` consecutive cues to translate plus ``context`` untranslated
    neighbours on either side, so the seam between two requests is never a cliff — the last
    lines of one request are the first request's context and vice versa. Nothing is asked
    for twice: a cue is `needed` in exactly one request.
    """
    size, context = max(1, int(size)), max(0, int(context))
    out = []
    for start in range(0, len(cues), size):
        end = min(start + size, len(cues))
        before = cues[max(0, start - context):start]
        after = cues[end:end + context]
        out.append([(cue, False) for cue in before]
                   + [(cue, True) for cue in cues[start:end]]
                   + [(cue, False) for cue in after])
    return out


def regroup(view, missing):
    """The same request again, asking only for the ids that didn't come back.

    Everything else in the view becomes context, which is what keeps a re-ask comparable to
    the first pass: the gap is still translated with the lines around it on the page, so the
    replacement line reads as part of the same scene rather than as an isolated sentence.
    """
    missing = set(missing)
    return [(cue, cue.index in missing) for cue, _ in view]
