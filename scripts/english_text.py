#!/usr/bin/env python3
"""english_text.py -- the one English-only gate for rendered text.

WHY A GATE AND NOT A PROMPT
---------------------------
The 2026-11-03 morning page carried a Devanagari headline. It came from the
collector verbatim (the no-AI fallback), so no prompt could have stopped it, and
the page loads no Devanagari face: the browser drew an empty box where the text
was. A rule that can only be applied after the text exists belongs in code.

THE RULE
--------
Text may be rendered only if every character belongs to a script the page's font
stack can draw. Latin (including accented letters), digits, punctuation and emoji
pass. Every non-Latin script fails, and the caller gets its name for the log.

NOT attempted: language detection. Text in Nepali but written in Latin letters
passes -- and it also RENDERS, which is the only property this gate promises.
"""

import re

#: (script name, first code point, last code point), inclusive. Only scripts the
#: page cannot draw are listed; emoji are symbols, not a script, and are allowed.
NON_LATIN_RANGES = (
    ("greek", "\u0370", "\u03ff"),
    ("cyrillic", "\u0400", "\u04ff"),
    ("armenian", "\u0530", "\u058f"),
    ("hebrew", "\u0590", "\u05ff"),
    ("arabic", "\u0600", "\u06ff"),
    ("syriac", "\u0700", "\u074f"),
    ("thaana", "\u0780", "\u07bf"),
    ("devanagari", "\u0900", "\u097f"),
    ("bengali", "\u0980", "\u09ff"),
    ("gurmukhi", "\u0a00", "\u0a7f"),
    ("gujarati", "\u0a80", "\u0aff"),
    ("oriya", "\u0b00", "\u0b7f"),
    ("tamil", "\u0b80", "\u0bff"),
    ("telugu", "\u0c00", "\u0c7f"),
    ("kannada", "\u0c80", "\u0cff"),
    ("malayalam", "\u0d00", "\u0d7f"),
    ("sinhala", "\u0d80", "\u0dff"),
    ("thai", "\u0e00", "\u0e7f"),
    ("lao", "\u0e80", "\u0eff"),
    ("tibetan", "\u0f00", "\u0fff"),
    ("myanmar", "\u1000", "\u109f"),
    ("georgian", "\u10a0", "\u10ff"),
    ("hangul_jamo", "\u1100", "\u11ff"),
    ("ethiopic", "\u1200", "\u137f"),
    ("cherokee", "\u13a0", "\u13ff"),
    ("khmer", "\u1780", "\u17ff"),
    ("hiragana", "\u3040", "\u309f"),
    ("katakana", "\u30a0", "\u30ff"),
    ("bopomofo", "\u3100", "\u312f"),
    ("hangul", "\u3130", "\u318f"),
    ("cjk", "\u3400", "\u4dbf"),
    ("cjk", "\u4e00", "\u9fff"),
    ("hangul", "\uac00", "\ud7af"),
    ("cjk", "\uf900", "\ufaff"),
)


def non_english_script(text):
    """The first non-Latin script name in `text`, or "" when there is none."""
    for ch in str(text or ""):
        for name, first, last in NON_LATIN_RANGES:
            if first <= ch <= last:
                return name
    return ""


def looks_english(text):
    """True when `text` can render in the page's own font stack."""
    return not non_english_script(text)


def held_back_note(count):
    """The sentence a payload's notes carry when stories were refused."""
    if count == 1:
        return "1 story was held back because it is not in English."
    return "%d stories were held back because they are not in English." % count


#: The two shapes `held_back_note` can produce, as one pattern. Used to WRITE one
#: sentence with `replace_held_back`, and to read the count off OLD payloads that
#: predate the `held_back` field.
_HELD_BACK_RE = re.compile(
    r"(\d+) stor(?:y was|ies were) held back because "
    r"(?:it is|they are) not in English\.")


def held_back_from_notes(notes):
    """LEGACY: how many stories the notes say were held back, read back off the text.

    Only for a payload written before `held_back` existed on it. New payloads carry
    the count as data, because a number that lives in prose is a number that can be
    re-derived differently by each reader -- which is exactly what happened: one page
    stated two counts for the same edition.
    """
    match = _HELD_BACK_RE.search(str(notes or ""))
    return int(match.group(1)) if match else 0


def replace_held_back(notes, count):
    """The notes with exactly ONE held-back sentence, stating `count`.

    Two stages can withhold a story for its script -- the deriver and the validator --
    and each used to APPEND its own sentence. A payload that passed through both
    therefore carried two different numbers in one field, and every reader picked a
    different one: the page regexed the first, the visible capped note kept whichever
    fit in the limit. Writing the sentence instead of appending to it is what makes
    "one count" true rather than hoped for.
    """
    text = _HELD_BACK_RE.sub("", str(notes or ""))
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not count:
        return text
    return (text + " " + held_back_note(count)).strip()
