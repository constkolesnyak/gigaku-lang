"""AnkiConnect client — the only way this side touches the collection.

`collection.anki2` is Anki's while Anki is running, so we never open it. AnkiConnect
(add-on 2055492159) binds 127.0.0.1:8765 with no API key and speaks a flat
``{"action", "version", "params"}`` JSON protocol, which is a dozen lines of stdlib —
no `requests`, and gigaku's dependency list stays where it is.

Everything that can be batched is: `notesInfo` over ~77k notes and `updateNoteFields`
over thousands of writes both go out in chunks, and the writes ride AnkiConnect's own
``multi`` action so a run is a handful of round trips rather than one per note.
"""
import json
import urllib.error
import urllib.request

from lib import config
from lib.config import UserError

VERSION = 6
# Big enough that a full read is a few round trips, small enough that one reply stays a
# sane size — notesInfo returns *every* field, including the audio ones we don't want.
READ_CHUNK = 500
WRITE_CHUNK = 200
# notesInfo over 500 notes is comfortably sub-second locally; a slow one means Anki is
# busy (a recalc, a sync), and waiting beats failing the run.
TIMEOUT = 120


def call(action, **params):
    """One AnkiConnect action. Raises UserError for anything the user can act on."""
    payload = json.dumps(
        {"action": action, "version": VERSION, "params": params}
    ).encode()
    request = urllib.request.Request(
        config.ANKICONNECT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode())
    except urllib.error.URLError as exc:
        raise UserError(
            f"can't reach AnkiConnect at {config.ANKICONNECT_URL} ({exc.reason}). "
            f"Start Anki — `gigaku clarity` reads and writes through it."
        )
    # AnkiConnect always answers 200; failure is in the body.
    if body.get("error") is not None:
        raise UserError(f"AnkiConnect {action}: {body['error']}")
    return body.get("result")


def _multi(actions):
    """Run a list of {action, params} in one round trip, raising on the first failure.

    ``multi`` returns one entry per action; a failed one is a dict carrying "error"
    rather than an exception, so an unchecked multi silently drops writes.
    """
    if not actions:
        return []
    results = call("multi", actions=actions)
    for action, result in zip(actions, results or []):
        if isinstance(result, dict) and result.get("error") is not None:
            raise UserError(f"AnkiConnect {action['action']}: {result['error']}")
    return results or []


def find_notes(query):
    return list(call("findNotes", query=query) or [])


def notes_info(note_ids):
    """Full note records for `note_ids`, read in chunks. Order follows `note_ids`."""
    out = []
    for start in range(0, len(note_ids), READ_CHUNK):
        chunk = list(note_ids[start:start + READ_CHUNK])
        out.extend(call("notesInfo", notes=chunk) or [])
    return out


def update_fields(updates, on_progress=None):
    """Write ``{note_id: {field: value}}``, chunked. Returns the number of notes written."""
    items = list(updates.items())
    for start in range(0, len(items), WRITE_CHUNK):
        chunk = items[start:start + WRITE_CHUNK]
        _multi([
            {
                "action": "updateNoteFields",
                "params": {"note": {"id": note_id, "fields": fields}},
            }
            for note_id, fields in chunk
        ])
        if on_progress:
            on_progress(min(start + WRITE_CHUNK, len(items)), len(items))
    return len(items)


def add_tags(note_ids, tag):
    for start in range(0, len(note_ids), WRITE_CHUNK):
        call("addTags", notes=list(note_ids[start:start + WRITE_CHUNK]), tags=tag)


def remove_tags(note_ids, tag):
    for start in range(0, len(note_ids), WRITE_CHUNK):
        call("removeTags", notes=list(note_ids[start:start + WRITE_CHUNK]), tags=tag)


def model_field_names(model):
    return list(call("modelFieldNames", modelName=model) or [])


def add_field(model, field, description=""):
    """Append `field` to `model`. This bumps the collection's schema — see clarity.py.

    The description is best-effort: `modelFieldSetDescription` is a newer AnkiConnect
    action, and a field with no description is still a working field.
    """
    call("modelFieldAdd", modelName=model, fieldName=field)
    if description:
        try:
            call(
                "modelFieldSetDescription",
                modelName=model,
                fieldName=field,
                description=description,
            )
        except UserError:
            pass
