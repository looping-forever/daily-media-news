"""One source of truth for WHICH KIND of notification this is.

WHY THIS EXISTS. On a collapsed ntfy notification the owner sees a title and a
handful of tags, nothing else. Before this module, priority 4 was shared by the
morning digest, a non-CRITICAL breaking alert, and the RESULT-path self-check
warning; and the tag ``warning`` was shared by real alerts and by pipeline
gate notices. So "an alert" and "a digest" arrived looking identical, and the
only thing that separated them was the title text -- which is the first thing a
lock screen truncates.

Each class gets exactly ONE literal tag. It is a constant written here, never
assembled from the notification's own content, so no model and no fetched page
can influence it. That is deliberate: the repo rule is that the AI must never
regain control of an honesty signal, and a class tag derived from the text it
is labelling would be exactly that failure.

THE SHAPE IS A HARD CONTRACT, NOT A STYLE CHOICE. ntfy answers a JSON STRING in
``Priority`` or ``Tags`` with HTTP 400 / code 40024. Measured 2026-10-12 against
a throwaway topic and recorded at ``deploy/cloudflare-worker.js``::

    {"priority":"4","tags":"newspaper"}     -> HTTP 400
    {"priority":4,  "tags":["newspaper"]}   -> HTTP 200

That day took production down for a digest. ``wire_shape`` below is the ONE
function the Python notification sites go through, so a site cannot drift back
into sending a string by accident: it raises instead.
"""

DIGEST = "digest"
BREAKING = "breaking"
RESULT = "result"
PIPELINE = "pipeline"

#: Every class tag this module knows about. A test pins the mapping against the
#: real call sites, so a new notification cannot appear unlabelled.
CLASS_TAGS = (DIGEST, BREAKING, RESULT, PIPELINE)


def with_class_tags(class_tag, tags=()):
    """`tags` with `class_tag` forced to the front, as an ARRAY OF STRINGS.

    Always returns a list, never a string: a bare string is the exact shape
    ntfy answers with HTTP 400. A bare STRING passed in place of a list is
    comma-split, matching the Worker's own coercion in ``digestPayload`` -- a
    caller that writes ``"warning"`` means one tag, and iterating the string
    instead would silently produce ``['w','a','r',...]``. Duplicates are
    dropped so re-tagging a site cannot grow the array without limit, and a
    non-string extra is coerced rather than passed through, because
    ``json.dumps`` would happily emit the number 5 and ntfy would still refuse
    the message.
    """
    if isinstance(tags, str):
        items = tags.split(",")
    else:
        items = list(tags or ())
    out = []
    for value in [class_tag] + items:
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if value and value not in out:
            out.append(value)
    return out


def priority_for(value, default=4):
    """`value` as a NUMBER ntfy accepts, or `default`.

    A numeric string is coerced; anything unrecognised falls back rather than
    raising, because a notification that is 400'd by a malformed priority is
    indistinguishable on the owner's phone from no notification at all.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1 or number > 5:
        return default
    return number


def wire_shape(topic, title, message, class_tag, tags=(), priority=4):
    """The JSON-ready ntfy body for a Python-side send.

    This is the function that makes the 400 unrepresentable: `tags` is a list of
    strings and `priority` is an int by construction, whatever the caller passed.
    """
    return {
        "topic": topic,
        "title": title,
        "message": message,
        "tags": with_class_tags(class_tag, tags),
        "priority": priority_for(priority),
    }