"""The score cache, and the field value it turns into. Pure — no SDK, no Anki.

Keyed by ``sha1(sentence, morph)`` rather than by note id (see select.Card.key), so the
cache survives the things that routinely churn note ids: a re-import, a cleared field, a
card dropping out of i+1 and coming back. Writes are atomic because a run can be Ctrl-C'd
in the middle of a batch, and a truncated cache would re-bill every card in it.
"""
import json
import os
import tempfile

from lib import config
from lib.anki.prompt import SCALE
from lib.anki.select import fold


VERSION = 4  # v1 held 0..10; v2 holds 0..100 (prompt.SCALE); v3 adds the run log;
             # v4 stamps each score with the rubric that produced it


def _document(path=None):
    """The whole cache file. Holds the scores *and* the run log, so both stay in one
    atomic write — a run number stamped on a card whose score never landed would be a
    worse lie than no stamp at all."""
    path = path or config.CLARITY_CACHE
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_runs(path=None):
    """``{run number: {"model": ..., "started": ...}}`` — what each stamp on a card means."""
    runs = _document(path).get("runs")
    return runs if isinstance(runs, dict) else {}


def start_run(model, rubric=None, path=None):
    """Claim the next run number and record what it is. Returns the number.

    `rubric` is prompt.FINGERPRINT — the wording that judged these cards. Kept because a
    rubric edit makes old and new scores incomparable *inside a word*, which is the only
    place they are ever compared, and a run that cannot say which rubric it used leaves
    that undetectable.
    """
    doc = _document(path)
    runs = doc.get("runs") if isinstance(doc.get("runs"), dict) else {}
    number = max((int(k) for k in runs), default=0) + 1
    runs[str(number)] = {"model": model, "started": _now(), "rubric": rubric}
    doc["runs"], doc["version"] = runs, VERSION
    doc.setdefault("scores", {})
    _write_document(doc, path)
    return number


def _now():
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def last_rubric(path=None):
    """The rubric fingerprint of the most recent run that recorded one, or None."""
    runs = read_runs(path)
    for key in sorted(runs, key=lambda k: int(k), reverse=True):
        rubric = (runs[key] or {}).get("rubric")
        if rubric:
            return rubric
    return None


def read(path=None):
    data = _document(path)
    scores = data.get("scores")
    if not isinstance(scores, dict):
        return {}
    if data.get("version", 1) < 2:
        # The scale widened from 0..10 to 0..100 so a word's candidates could be given
        # distinct scores (see prompt.SCALE). Rescaling beats re-asking: it keeps ten
        # thousand already-judged cards, and ×10 is exactly what the old value meant.
        return {key: int(value) * 10 for key, value in scores.items()}
    return scores


def read_rubrics(path=None):
    """``{key: rubric fingerprint}`` — which wording judged each cached score.

    Kept **in the cache, not on the card**, and that is the whole point of it. The obvious
    place is `My Run` in Anki, which already names the run; but the one operation that needs
    this answer — re-score everything an old rubric judged — is also the operation that
    clears that field, and a selection that reads a field it is about to blank matches
    nothing on the second pass. (Measured the hard way: a re-score that emptied 2,855 cards
    and scored none of them.) The cache is written *after* the scoring it describes, so it
    cannot be read stale by the pass that rewrites it.
    """
    rubrics = _document(path).get("rubrics")
    return rubrics if isinstance(rubrics, dict) else {}


def stale(cache, rubrics, current):
    """Keys whose score came from a different rubric — or from one nobody recorded.

    Unknown counts as stale on purpose: "we cannot say which wording produced this" is not
    a reason to keep comparing it against numbers we can vouch for. After a stamped run
    the only unknowns left are keys whose card has drifted out of i+1, which cost nothing
    until the card comes back.
    """
    return {key for key in cache if rubrics.get(key) != current}


def write(scores, path=None, rubrics=None):
    """Persist the scores, leaving the run log alone."""
    doc = _document(path)
    doc["version"], doc["scores"] = VERSION, scores
    doc.setdefault("runs", {})
    if rubrics is not None:
        doc["rubrics"] = {k: v for k, v in rubrics.items() if k in scores}
    else:
        doc.setdefault("rubrics", {})
    _write_document(doc, path)


def _write_document(doc, path=None):
    path = path or config.CLARITY_CACHE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=os.path.dirname(path), delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(doc, handle, ensure_ascii=False)
        os.replace(handle.name, path)
    except BaseException:
        os.unlink(handle.name)
        raise


def merge(scores, new):
    """Fold ``{key: 0..SCALE}`` into the cache. Later runs win — a re-score is a correction."""
    merged = dict(scores)
    merged.update({k: int(v) for k, v in new.items()})
    return merged


def field_value(score):
    """0..SCALE → the "0.00".."1.00" the My Clarity field holds.

    Two decimals rather than one: the field reads as the 0..1 number it is, and the sort
    key derives from the same value, so what you see in the browser and what orders the
    alternates can't drift.
    """
    return f"{int(score) / SCALE:.2f}"


COUNTER_PAD, CLARITY_PAD, ALLCOUNT_PAD = 6, 3, 4


def sort_key(alternates, morph, score, all_count):
    """`My Sort Key` — the add-on's format, repeated here.

    The add-on (anki/study_morph_counter) owns this string and rewrites every i+1 note on
    each AnkiMorphs recalc. Writing it here too is a convenience, not a second owner: the
    values are identical, and if they ever drifted the next recalc would overwrite ours.
    Without it a scored card keeps a stale key until you remember to press R, which is a
    silly thing to have to remember.

    Clarity sits after the morph deliberately — see the add-on's docstring: that placement
    is what leaves my-learn ordered by how common a word is, while deciding the order
    *inside* one word's alternates, which is the only place the comparison means anything.
    The morph is `select.fold`ed like the add-on's copy: with case intact every `Leben`
    card sorted ahead of every `leben` card before clarity got a say, and to the
    case-insensitive L view those are one word.
    """
    return (f"{alternates:0{COUNTER_PAD}d} {fold(morph)} "
            f"{score:0{CLARITY_PAD}d} {all_count:0{ALLCOUNT_PAD}d}")


def sort_key_updates(cards, scores, *, sort_field):
    """Bring **every** card's sort key to the four-part format. Returns {note_id: {field}}.

    Not an optimisation — a correctness fix. The key is compared as a *string*, so a card
    still carrying the old three-part `{n} {morph} {all_count}` cannot be ordered against a
    four-part `{n} {morph} {clarity} {all_count}`: after the morph one has `0008` where the
    other has `045 `, and the comparison runs off into unrelated digits. Mixing the two
    inside one word — which is exactly what happens when only the scored cards get rewritten
    — shuffles the alternates view into nonsense.

    The add-on normalises all of them on its next recalc; doing it here means a scoring run
    leaves the collection consistent straight away rather than half-converted. Unscored
    cards get `000`, which sorts them below every judged one and leaves am-all-morphs-count
    ordering them among themselves, exactly as before the field existed.
    """
    out = {}
    for card in cards:
        if not card.alternates or not card.morph:
            continue
        key = sort_key(card.alternates, card.morph,
                       scores.get(card.key, 0), card.all_count)
        if card.sort != key:
            out[card.note_id] = {sort_field: key}
    return out


def updates(cards, scores, *, clarity_field, sort_field=None, run_field=None, run=None):
    """``{note_id: {field: value}}`` for cards whose stored value is out of date.

    Only differences are written: a re-run over a fully-cached word must be zero
    collection writes, or every run would churn the sync.
    """
    out = {}
    for card in cards:
        score = scores.get(card.key)
        if score is None:
            continue
        fields = {}
        value = field_value(score)
        if card.clarity != value:
            fields[clarity_field] = value
        if sort_field and card.alternates:
            key = sort_key(card.alternates, card.morph, score, card.all_count)
            if card.sort != key:
                fields[sort_field] = key
        if run_field and run is not None and card.run != str(run):
            fields[run_field] = str(run)
        if fields:
            out[card.note_id] = fields
    return out


def as_fraction(scores, cards):
    """``{note_id: 0.0..1.0}`` for the cards we have a score for — the calibration view."""
    out = {}
    for card in cards:
        score = scores.get(card.key)
        if score is not None:
            out[card.note_id] = int(score) / SCALE
    return out
