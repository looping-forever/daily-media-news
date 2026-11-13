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


def held_back_note(count, pool=0):
    """The sentence a payload's notes carry when something was refused.

    `pool` names the POPULATION the count was taken from, and the sentence says
    so. With a pool the count is candidates the deriver withheld for their
    script; without one it is stories this pipeline dropped. The two are never
    added together: run 43246133293 withheld 7 of 53 candidates and the edition
    note said "7 stories were held back" over a 15-card edition, which balances
    against no pool at all (53-7=46, 20-7=13, the edition carried 15).
    """
    count = int(count or 0)
    pool = int(pool or 0)
    if count <= 0:
        return ""
    if pool >= count:
        if count == 1:
            return ("1 of the %d collected candidates was held back because its "
                    "title is not in English." % pool)
        return ("%d of the %d collected candidates were held back because their "
                "titles are not in English." % (count, pool))
    if count == 1:
        return "1 story was held back because it is not in English."
    return "%d stories were held back because they are not in English." % count


#: The story-population shapes, and the candidate-population shapes, as patterns.
#: Used to WRITE one sentence each with `replace_held_back`, and to READ the count
#: off OLD payloads that predate the `held_back` field.
_HELD_BACK_RE = re.compile(
    r"(\d+) stor(?:y was|ies were) held back because "
    r"(?:it is|they are) not in English\.")
_HELD_BACK_CANDIDATE_RE = re.compile(
    r"\d+ of the \d+ collected candidates? (?:was|were) held back because "
    r"(?:its title is|their titles are) not in English\.")


def held_back_from_notes(notes):
    """LEGACY: how many stories the notes say were held back, read back off the text.

    Only for a payload written before `held_back` existed on it. New payloads carry
    the count as data, because a number that lives in prose is a number that can be
    re-derived differently by each reader -- which is exactly what happened: one page
    stated two counts for the same edition.
    """
    match = _HELD_BACK_RE.search(str(notes or ""))
    return int(match.group(1)) if match else 0


def replace_held_back(notes, count, pool=0, stories=0):
    """The notes with exactly ONE held-back sentence per population.

    Two stages can withhold something for its script -- the deriver withholds
    CANDIDATES, the validator drops STORIES -- and each used to append its own
    sentence. A payload that passed through both therefore carried two different
    numbers in one field, and every reader picked a different one: the page
    regexed the first, the visible capped note kept whichever fit in the limit.
    Now each population gets its own sentence, naming itself, written here and
    never appended elsewhere.
    """
    text = _HELD_BACK_CANDIDATE_RE.sub("", str(notes or ""))
    text = _HELD_BACK_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    for sentence in (held_back_note(count, pool=pool),
                     held_back_note(stories)):
        if sentence:
            text = (text + " " + sentence).strip()
    return text



def floor_note(kept, required):
    """The sentence a short edition carries: how many cards, against what floor."""
    return ("Short edition: %d stories, below the %d-story floor."
            % (int(kept), int(required)))


#: Statement, or its absence, as one pattern -- `replace_floor` rewrites it so the
#: repair pass cannot leave two different counts in one field.
_FLOOR_RE = re.compile(
    r"Short edition: \d+ stories, below the \d+-story floor\.")


def replace_floor(notes, kept, required):
    """The notes with exactly ONE short-edition sentence, or none when the floor is met."""
    text = _FLOOR_RE.sub("", str(notes or ""))
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not required or int(kept) >= int(required):
        return text
    sentence = floor_note(kept, required)
    return (sentence + " " + text).strip() if text else sentence
