"""Every Anki search string this add-on writes or recognises — pure, no aqt.

Config values (notetype, field and tag names) arrive as arguments: the callers own the
config read, this module owns the string shapes. Recognisers live beside the builders they
match (`alt_query`/`is_alt_query`, `context_query`'s shape/`is_context_query`) because a
drilled-in view is recognised *by the query that built it* — that pairing is the whole
navigation design, so the two halves must never drift apart.
"""


def search_escape(text):
    """Escape a value for an Anki search token wrapped in double quotes."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def all_query(notetype, ready_tag):
    """Every i+1 card of the notetype."""
    return f'note:"{notetype}" tag:"{ready_tag}"'


def missing_query(notetype, ready_tag, counter_field, sort_field):
    """i+1 cards still lacking the counter OR the sort key (the startup catch-up set)."""
    return (
        f'{all_query(notetype, ready_tag)} '
        f'(-"{counter_field}:_*" OR -"{sort_field}:_*")'
    )


def stale_learn_query(notetype, learn_tag, ready_tag):
    """Notes still tagged my-learn that are no longer i+1 — marked known, or bumped to i+≥2."""
    return f'note:"{notetype}" tag:"{learn_tag}" -tag:"{ready_tag}"'


def learn_tag_query(notetype, learn_tag):
    return f'note:"{notetype}" tag:"{learn_tag}"'


def study_deck_query(notetype, study_deck):
    return f'note:"{notetype}" deck:"{search_escape(study_deck)}"'


def alt_query(notetype, study_field, word):
    """Every card studying `word`, in each shape MvJ writes am-study-morphs.

    A field search is a whole-field match, so the bare-morph query alone misses a card
    whose field 💣 rewrote into ``Sentence: <word> | Definition: <...>`` — i.e. it dropped
    the very card L was pressed on out of its own alternates list. Measured on the live
    collection for 撮る: bare 204, union 205. A ``*<word>*`` substring match would be 252,
    sweeping in cards where the word is somebody else's *definition* morph, so the shapes
    are spelled out.
    """
    w = search_escape(word)
    return (
        f'note:"{notetype}" '
        f'("{study_field}:{w}" OR "{study_field}:Sentence: {w}" OR "{study_field}:Sentence: {w} |*")'
    )


def canon(query):
    """A search string with Anki's re-quoting undone, for comparisons only.

    Anki round-trips a query given to `Browser(search=…)` through its parser and shows
    the rebuilt string — measured 2026-08-06, the day J's query grew a note clause:
    `note:"🇯🇵 MvJ" (tag:my-learn)` came back as `"note:🇯🇵 MvJ" (tag:my-learn)` and the
    byte-compare called the queue view *foreign*, which is how J trashed the user's
    remembered columns. Where the quotes sit is the parser's mood; what the query says is
    not — so every recogniser compares with the quotes dropped and the whitespace
    collapsed. Never feed a canon()ed string back into a search: it is for equality, the
    quotes are load-bearing in a real query.
    """
    return " ".join((query or "").replace('"', "").split())


def is_alt_query(notetype, study_field, query):
    """Whether the Browser is showing an alternates view this add-on built."""
    return canon(query).startswith(canon(f'note:"{notetype}" ("{study_field}:'))


def is_context_query(sentence_audio_field, query):
    """Whether the Browser is showing MvJ's O (View Context) episode search.

    MvJ builds it as ``note:"<notetype>" "<Sentence Audio field>:*<show>_<episode>*"``
    since 2026-08-08 (a German and a Japanese rip of one show share the filename prefix,
    so the unscoped search braided the two languages' episodes together) — and as the
    bare form before that. Both are recognised: the scoping and this recogniser changed
    in lockstep, and a view opened before the change can outlive it. The field name
    comes from MvJ's own config rather than assumed, since it is configurable.
    Recognising it is what lets L treat the context view as somewhere to come *out* of.
    """
    c = canon(query)
    marker = canon(f'"{sentence_audio_field}:*')
    return c.startswith(marker) or (c.startswith("note:") and marker in c)


def home_query(mvj_query, ready_tag, notetype=""):
    """The main working search: MvJ's browser query, or the ready tag if it has none.

    Shared by J (which opens the Browser on it) and by L's way out of an alternates view
    when it has nothing remembered — the two must not drift into disagreeing about "the
    main view".

    Scoped to one note type when one is given: my-learn and the `_card-status` tags are
    shared across languages (one collection, one AnkiMorphs), so an unscoped queue view
    mixes them — measured the day German landed (2026-08-06): 755 German notes sitting
    inside the Japanese J view. Languages are never mixed in a queue (user's rule).
    """
    body = (mvj_query or "").strip() or f"tag:{ready_tag}"
    return f'note:"{notetype}" ({body})' if notetype else body
