#!/usr/bin/env python3
"""payload_io.py -- the ONE payload parse contract for the academic monitor.

WHY THIS FILE EXISTS
--------------------
The tracked semester-1 PASS (the roll itself is a private identifier and
belongs in no committed file) was announced in a run that delivered
NOTHING and still exited 0. The chain was:

  * ``emit_notification`` printed every payload with ``indent=2``;
  * the workflow ``tee``'d the concatenation into ONE file;
  * ``payload_check`` only asked "is the file non-empty?";
  * the ntfy step called ``json.load`` on the whole blob, got
    ``JSONDecodeError: Extra data: line 33`` from the FOUR payload
    documents, and -- inside a ``try/except`` -- printed "No notification
    payload available - ntfy skipped";
  * ntfy, the render step and the Cloudflare Pages deploy all keyed off
    that same skip, so the whole delivery half of the job no-opped;
  * the job exited 0.

There was a second, quieter bug in the same loop: ``payload = data[-1]``.
Even if the file HAD parsed, payloads 1..N-1 would have been dropped on
the floor and only the last one delivered.

THE CONTRACT
------------
1. FORMAT. The live monitor writes JSON-Lines: one COMPACT JSON object per
   line (``separators=(",", ":")``), so a payload can never span lines and
   four payloads can never fuse into one unparseable blob. The dry-run
   preview file is a JSON LIST; this reader accepts both, because the
   preview file is a real, live, human-inspected artifact.

2. TOTAL OR RAISE. ``read_payloads`` is total in the sense that it always
   returns a list or raises -- it NEVER skips a line it could not parse and
   NEVER half-accepts a file. That is the entire point: today's
   ``try/except`` around ``json.load`` converts a parse error into a green
   run, which is the failure this module exists to make impossible. A
   caller that catches ``PayloadFormatError`` and continues is the bug; a
   caller that turns it into ``::error::`` and a non-zero exit is the fix.

3. THE RULES, EXACTLY (FROZEN).
   - file absent                      -> ``[]``   (not an error)
   - file present, 0 non-blank lines  -> ``[]``   (not an error)
   - blank / whitespace-only lines    -> skipped
   - any non-blank line failing
     ``json.loads``                   -> ``PayloadFormatError(path, line,
                                           "invalid json")``
   - any parsed line that is not a
     dict                             -> ``PayloadFormatError(path, line,
                                           "not an object")``
   - first non-blank char ``'['``    -> legacy JSON list: ``json.loads``
                                           the whole document, which must
                                           be a list of dicts
   This is deliberately NOT a heuristic that "tries json.load, and if that
   fails tries JSON-Lines". A file is either one of the two documented
   shapes or it is a broken file, and a broken file must be loud.

4. NO CONSUMER PARSES ON ITS OWN. ``payload_check``, the ntfy step,
   ``render_academic.py --payload`` and the ``delivery_assert`` step all go
   through ``read_payloads``. A consumer that keeps its own ``json.load``
   re-opens the exact hole this closes.

5. EVERY PAYLOAD IS DELIVERED. There is no ``[-1]`` anywhere in this
   module, and the ntfy budget is PER PAYLOAD, not aggregate: each payload
   gets its own message with its own 4096-byte budget, which preserves the
   existing single-payload contract exactly while fixing the multi-payload
   case. A payload too large for one message has its MESSAGE BODY
   truncated (never its title) and is marked ``truncated`` in the delivery
   record -- dropped, never.

Stdlib only.
"""

import json

__all__ = [
    "PayloadFormatError",
    "read_payloads",
    "count_payloads",
    "payload_to_ntfy_body",
    "fit_ntfy_body",
    "MAX_NTFY_BYTES",
    "TRUNCATION_MARKER",
]

#: ntfy's hard per-message ceiling, in bytes of the POSTED body.
MAX_NTFY_BYTES = 4096

#: Appended to a message body that had to be shortened. It is part of the
#: message, so the byte budget accounts for it.
TRUNCATION_MARKER = "[message truncated to fit the ntfy 4096-byte limit]"


class PayloadFormatError(Exception):
    """A payload file that is neither a valid JSON-Lines nor a valid list.

    Carries ``.path``, ``.line`` (1-based) and ``.reason`` so a caller can
    report exactly WHICH file and WHICH line is broken instead of collapsing
    four unrelated failures into "no payload available".
    """

    def __init__(self, path, line, reason):
        self.path = path
        self.line = line
        self.reason = reason
        super().__init__("payload file %s line %s: %s" % (path, line, reason))


def _legacy_list(path, text):
    """A JSON-list payload file (the dry-run preview format)."""
    try:
        data = json.loads(text)
    except ValueError:
        raise PayloadFormatError(path, 1, "invalid json") from None
    if not isinstance(data, list):
        raise PayloadFormatError(path, 1, "not a list of objects")
    payloads = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise PayloadFormatError(path, index, "not an object")
        payloads.append(item)
    return payloads


def read_payloads(path):
    """Every payload in `path`, in file order. Total, or raises.

    Returns ``[]`` for a missing file and for a file with no non-blank
    lines -- "nothing was emitted" is a real, benign state. Raises
    ``PayloadFormatError`` for anything else; the caller decides whether
    that is a `::error::` (it always should be).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return []
    if not text.strip():
        return []
    if text.lstrip()[0] == "[":
        return _legacy_list(path, text.lstrip())

    payloads = []
    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise PayloadFormatError(path, line_no, "invalid json") from None
        if not isinstance(obj, dict):
            raise PayloadFormatError(path, line_no, "not an object")
        payloads.append(obj)
    return payloads


def count_payloads(path):
    """Number of payloads in `path`, or -1 when the file does not exist.

    -1 (not 0) so a caller can tell "no file, therefore nothing was
    promised" apart from "the file exists and is empty", which is a
    different statement about the same run.
    """
    try:
        open(path, "r", encoding="utf-8").close()
    except FileNotFoundError:
        return -1
    return len(read_payloads(path))


def payload_to_ntfy_body(payload, topic=""):
    """The payload's ntfy block, as the POST body. Moved here verbatim.

    `topic` is the secret, never the payload's own value, so a payload
    cannot redirect a notification to a topic of its own choosing.
    """
    ntfy = payload.get("ntfy") if isinstance(payload, dict) else None
    if not isinstance(ntfy, dict):
        ntfy = {}
    return {
        "topic": topic,
        "title": ntfy.get("title") or "",
        "message": ntfy.get("message") or "",
        "tags": ntfy.get("tags") or [],
        "priority": ntfy.get("priority") or 3,
        "click": ntfy.get("click") or "",
        "actions": ntfy.get("actions") or [],
    }


def _post_bytes(body):
    """Byte length of the body AS POSTED (the same default dumps the
    workflows use, so the budget can never disagree with the wire)."""
    return len(json.dumps(body).encode("utf-8"))


def fit_ntfy_body(body, max_bytes=MAX_NTFY_BYTES):
    """Return ``(body, truncated)`` with the POST body within `max_bytes`.

    Only the MESSAGE BODY is shortened, and only when the body alone does
    not fit. The title, the tags and the priority are never touched: the
    title is what tells the owner WHICH semester this is, and a digest that
    arrives with a truncated title is a different lie than one that arrives
    with a truncated body. A payload that is too big is still DELIVERED and
    still marked `truncated` -- silently dropping it is the failure this
    whole contract exists to remove.
    """
    fitted = dict(body)
    if _post_bytes(fitted) <= max_bytes:
        return fitted, False

    message = fitted.get("message") or ""
    if not isinstance(message, str):
        message = str(message)
    suffix = "\n\n" + TRUNCATION_MARKER

    # Longest prefix of `message` that still fits, found by bisection. The
    # slice is on CHARACTERS, so a multi-byte character is never cut in half.
    low, high, best = 0, len(message), None
    while low <= high:
        mid = (low + high) // 2
        candidate = dict(fitted)
        candidate["message"] = message[:mid] + suffix
        if _post_bytes(candidate) <= max_bytes:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1

    if best is None:
        # The envelope ALONE (title + tags + topic) exceeds the budget. The
        # title still wins: it is never truncated, and the marker is the
        # message.
        best = dict(fitted)
        best["message"] = TRUNCATION_MARKER
    return best, True
