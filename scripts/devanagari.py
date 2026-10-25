#!/usr/bin/env python3
"""scripts/devanagari.py — Devanagari recognition for the academic result path.

WHY THIS MODULE EXISTS
----------------------
Verified against the live IOST result feed on 2026-09-29: **5 of the 14
notices on the result feed are written in Nepali (Devanagari script)**. The
result pass read those titles with a Latin-only matcher, got ``None``, recorded
``irrelevant`` / ``no_semester_match`` — and under the no-burn invariant an
``irrelevant`` is a PERMANENT decision, so the floor advanced and the notice was
never read again. Notice 15696 was already burned exactly this way. The same
class of bug that cost the owner his semester-1 pass has one layer up and
would have cost him his semester-4 result.

WHAT IS AND IS NOT TRANSLITERATED
---------------------------------
**Digits only, and the mapping is exhaustive and exactly reversible.**

There is deliberately NO Devanagari->Latin WORD transliterator here. A
transliterator needs a lexicon, a longest-match segmenter and a set of
morphology rules, and a single wrong segment silently produces a plausible
wrong semester: ``चौथो`` mis-segmented becomes "4th" for the right answer by
luck, but ``तेस्रो`` mis-segmented can become "3" or "8" with equal
plausibility, and a WRONG semester on a result notice is a wrong verdict about
the owner's degree. So the semester WORD is looked up in an explicit table
(:data:`NEPALI_SEMESTER_WORDS`) that maps the word **directly to an int**,
bypassing transliteration entirely. Latin academic vocabulary already verified
against this board (``B.Sc.CSIT``, ``Fourth Semester``, ``Result Published``) is
reused by feeding digit-latinised text to the module's own existing regexes
rather than by writing a second matcher — see ``classify_semester`` in
``check_academic_result.py``.

Nothing here decides anything. This module only answers "what script is this,
which words of our vocabulary are present, and which semester does an ordinal
word denote". The decision, and the fail-safe around an unreadable title, live
in :func:`check_academic_result.classify_semester`.

Stdlib only.
"""

import re

# ---------------------------------------------------------------------------
# Script detection
# ---------------------------------------------------------------------------

# The Devanagari block (U+0900-U+097F), which is what Nepali is written in.
# Extended ranges (Devanagari Extended, U+A8E0+, for older conjunct forms) are
# deliberately NOT included: they are not used in the board's titles, and a
# wider class would misfire on scripts that merely share the block.
DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")

# Nepali digits, U+0966..U+096F, written out in full because the contract
# fixes these ten entries. Exhaustive and exactly reversible: every key is a
# single Devanagari digit and every value is the ASCII digit of the same
# value, so latinize_digits(latinize_digits(t)) == t for digit content and no
# information is lost in either direction.
NEPALI_DIGITS = {
    "०": "0",
    "१": "1",
    "२": "2",
    "३": "3",
    "४": "4",
    "५": "5",
    "६": "6",
    "७": "7",
    "८": "8",
    "९": "9",
}

# What latinize_digits leaves behind: the Devanagari block, EXCEPT the ten
# digits above. Used only where a whole-word boundary is needed.
_NON_DIGIT_DEVANAGARI_RE = re.compile(r"[\u0900-\u0965\u096f-\u097f]")


def latinize_digits(text):
    """Replace every Nepali digit with its ASCII counterpart.

    This is the ONLY transliteration in the academic path. It is exact and
    reversible, which is why it is safe: it cannot invent or lose a semester.
    Non-digit characters (including Devanagari letters) are left untouched, so
    a title can be digit-latinised and then fed to the module's existing,
    already-verified ASCII-year regexes.

    ``"परीक्षा नतिजा २०८१"`` -> ``"परीक्षा नतिजा 2081"``.
    """
    if not text:
        return ""
    if not DEVANAGARI_RE.search(text):
        return text
    out = []
    for ch in text:
        out.append(NEPALI_DIGITS.get(ch, ch))
    return "".join(out)


def digit_boundary_projection(text):
    """Latinise digits AND blank every other Devanagari character to a space.

    Why this exists in addition to :func:`latinize_digits`: the ASCII regexes
    this project trusts use ``\\b``, and ``\\b`` in Python's Unicode-aware
    ``re`` is defined between a word and a non-word character — a Devanagari
    letter IS a word character. So in ``"वर्ष२०८१"`` the digit run has no word
    boundary on its left and ``\\b(20[6-9]\\d)\\b`` would MISS the year even
    after digit-latinisation. Replacing the surrounding Devanagari with spaces
    restores the boundary without transliterating a single word.

    Length is preserved, so a match offset in the projection is the same offset
    in the original.
    """
    if not text:
        return ""
    if not DEVANAGARI_RE.search(text):
        return text
    out = []
    for ch in text:
        if ch in NEPALI_DIGITS:
            out.append(NEPALI_DIGITS[ch])
        elif _NON_DIGIT_DEVANAGARI_RE.match(ch):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def detect_script(title):
    """Classify a title's script.

    Returns exactly one of ``"latin"``, ``"devanagari"``, ``"mixed"``,
    ``"empty"``. ``"empty"`` covers both a falsy title and a title with no
    Devanagari and no cased/alphabetic content at all (digits, punctuation).

    ``"mixed"`` is a real case on this board: titles such as
    ``"बी.एस.सी. सेमेस्टर नतिजा २०८१"`` carry an ASCII batch year and an ASCII
    abbreviation inside Devanagari prose. Both stages must run on those.
    """
    if not title or not str(title).strip():
        return "empty"
    text = str(title)
    has_dev = DEVANAGARI_RE.search(text) is not None
    has_lat = _has_latin_letter(text)
    if has_dev and has_lat:
        return "mixed"
    if has_dev:
        return "devanagari"
    if has_lat:
        return "latin"
    return "empty"


def _has_latin_letter(text):
    return any(("a" <= ch.lower() <= "z") for ch in text)


# ---------------------------------------------------------------------------
# Nepali vocabulary
# ---------------------------------------------------------------------------

# Semester ORDINAL WORDS -> the integer they denote, directly.
#
# Both the full ordinal adjective (पहिलो) and its stem (पहिल) are listed
# because TU writes the notice title in whichever form fits the line, and a
# substring check over both forms matches either. This table is the reason no
# word transliterator is needed: the answer is looked up, not derived.
NEPALI_SEMESTER_WORDS = {
    "पहिलो": 1, "पहिल": 1,
    "दोस्रो": 2, "दोस्र": 2,
    "तेस्रो": 3, "तेस्र": 3,
    "चौथो": 4, "चौथ": 4,
    "पाँचौं": 5, "पाँचौ": 5,
    "छैठौं": 6, "छैठौ": 6,
    "सातौं": 7, "सातौ": 7,
    "आठौं": 8, "आठौ": 8,
    # --- OBSERVED NON-STANDARD SPELLINGS, added 2026-09-29 from the live feed.
    # The board does NOT write standard Nepali. Notice 15696's real title
    # reads "चाैथाे" (fourth) where standard Nepali is "चौथो": the matra
    # ौ is typed as ा + े , the same way a legacy Nepali font or a
    # romanised-keyboard IME produces it. Without these entries the ordinal
    # word is invisible, the semester token is unresolved, and a CSIT result
    # published in the board's ACTUAL spelling resolves to no_match - which
    # under R4 is a permanent burn, the exact failure this module exists to
    # remove. These are looked up, never transliterated, like every other
    # entry: a table of observed spellings to integers cannot invent a
    # semester a human did not read off the page.
    "चाैथाे": 4, "चाैथ": 4,
    "पहिलोँ": 1, "पहिलो": 1,
    "दोस्रोँ": 2, "दोस्र": 2,
    "तेस्रोँ": 3, "तेस्र": 3,
    "पाँचाैं": 5, "पाँचाै": 5,
    "छैठाैं": 6, "छैठाै": 6,
    "साताैं": 7, "साताै": 7,
    "आठाैं": 8, "आठाै": 8,
}

# Longest-first so a longer word is tested before one that is a prefix of it
# (पहिलो before पहिल, चौथो before चौथ). Without this, dict order would decide.
NEPALI_SEMESTER_WORDS_LONGEST_FIRST = tuple(sorted(
    NEPALI_SEMESTER_WORDS, key=len, reverse=True))

# The Nepali word for "semester". Its PRESENCE alone carries no semester
# number — it is a context token, not an ordinal.
#
# "शत्र" is the board's own abbreviation, read off the live title of notice
# 15696 on 2026-09-29. It is a real token on real notices, not a guess.
NEPALI_SEM_TOKENS = ("सेमेस्टर", "शत्र")

# "result" and its relatives. Same reasoning as the Latin _RESULT_TERMS_RE:
# deliberately wide. A false positive costs one detail fetch; a false negative
# loses a result forever.
NEPALI_RESULT_TERMS = (
    "नतिजा", "प्रकाशित", "परीक्षा", "उत्तीर्ण", "अनुत्तीर्ण",
    "नतिजा प्रकाशित", "परीक्षा नतिजा",
)

# The CSIT markers this board writes in Nepali, i.e. the Nepali spelling of
# the same thing the Latin _MARKER_RE matches.
NEPALI_MARKER_TERMS = (
    "सी.एस.आई.टी", "सीएसआईटी", "सी.एस.आय.टी", "सीएसआयटी",
    "बी.एस.सी.", "बिएससी", "बी.एस.सी",
    # --- OBSERVED MISSING CSIT SPELLINGS, added 2026-09-30. The board writes
    # the degree as "बी.एस.आई.टी." at least as often as "सी.एस.आई.टी.", and
    # that form was MISSING here: `contains_any` is plain substring
    # containment, so a genuine Nepali CSIT result titled "बी.एस.आई.टी. ..." had
    # no marker at all, was classified no_marker -> no_match, was recorded
    # `irrelevant`, and the floor advanced past it: a BURN of a real result
    # purely because the abbreviation was spelled B.S. instead of S.Sc. The
    # transliterated "य" variants are added alongside the "ई" ones because the
    # existing table already carries both for the S.Sc. forms.
    "बी.एस.आई.टी", "बीएसआईटी", "बी.एस.आय.टी", "बिएसआईटी",
)

# NON-CSIT programme markers: proof the notice belongs to ANOTHER programme.
#
# This is a NEW, SEPARATE vocabulary and it must never be merged into
# NEPALI_MARKER_TERMS. A marker there means "this is our programme", so adding
# "M.Sc." there would turn every master's result into a CSIT match - the exact
# false-positive the CSIT marker exists to prevent. These terms are checked
# only in the negative direction (marker absent AND one of these present =>
# positive no_match), so they can only ever REJECT, never claim.
#
# Same rule as everywhere else in this module: PRESENCE ONLY. The list is here
# so that the "semester token + result term but no CSIT marker" defer case can
# still be a DECISION for the notices we have actually observed carrying
# another programme's name. Without it those notices would defer forever and
# freeze the floor on real, permanently irrelevant traffic.
#
# The master's forms are read off the live feed: notice 15696's real title
# begins "स्नातकाेत्तर तह" (master's level) in the board's own non-standard
# spelling (the matra is typed as two code points, exactly as in
# NEPALI_SEMESTER_WORDS). The standard spelling is listed too.
#
# EXTENDED 2026-10-03 with "विद्यावारिधि" (doctorate), read off notice 15515's
# real title "विद्यावारिधि प्रवेश परीक्षा केन्द्र र समयसम्बन्धी जरुरी सूचना".
# The list carried only the abbreviated spellings (पीएच.डी / पीएचडी), so the
# board's own full-word form of a PhD notice matched nothing and the doctorate
# programme was invisible to the filter.
NEPALI_NON_CSIT_MARKER_TERMS = (
    "स्नातकाेत्तर", "स्नातकोत्तर", "स्नातकाोत्तर", "स्नातकॆत्तर",
    "मास्टर", "पीएच.डी", "पीएचडी", "बी.ए.एल", "बी.ई.",
    "विद्यावारिधि",
)

# Administrative / non-result notices: "notice", "meeting", "tender",
# "appointment", "study", "holiday", "memorial", "cycle", "registration",
# "admission", "application", "ceremony", "training", "list", "information",
# "withdrawn", "postponed".
#
# PRESENCE ONLY. Each hit proves the notice is administrative, which is
# evidence ABOUT the notice and therefore a legitimate `no_match`. The ABSENCE
# of these terms proves NOTHING and must never be used to reject: this project
# has already burned a real result by treating a missing keyword as proof
# (see the `looks_provably_irrelevant` tombstone in check_academic_result.py).
# This list exists to close the ONE residual gap in the unreadable-title
# fail-safe — a Nepali non-result notice that carries no result or marker term
# would otherwise be unreadable to us and defer forever, freezing the floor.
NEPALI_NON_RESULT_TERMS = (
    "सूचना", "बैठक", "निविदा", "नियुक्ति", "अध्ययन", "छुट्टी", "स्मारक",
    "साइकिल", "रजिस्ट्र", "प्रवेश", "आवेदन", "समारोह", "प्रशिक्षण", "सूची",
    "जानकारी", "हटाइएको", "स्थगन",
)


def contains_any(text, terms):
    """True when any term in ``terms`` occurs in ``text`` (case-insensitive).

    Plain substring containment, no word boundaries: Devanagari orthography
    attaches postpositions directly (``नतिजा`` -> ``नतिजा प्रकाशित``) and a
    ``\\b``-anchored match would be wrong for both scripts here.
    """
    if not text or not terms:
        return False
    low = str(text)
    for term in terms:
        if term and term in low:
            return True
    return False


def nepali_semester_from_words(text):
    """The semester an ordinal Nepali word denotes, or None.

    Longest word first, so पहिलो is preferred over its own stem पहिल when both
    could match. The returned int comes straight out of
    :data:`NEPALI_SEMESTER_WORDS`; it is never derived by transliteration.
    """
    if not text:
        return None
    for word in NEPALI_SEMESTER_WORDS_LONGEST_FIRST:
        if word in text:
            return NEPALI_SEMESTER_WORDS[word]
    return None


def has_nepali_sem_token(text):
    """True when the Nepali word for "semester" is present (no number implied)."""
    return contains_any(text, NEPALI_SEM_TOKENS)


def has_nepali_marker(text):
    return contains_any(text, NEPALI_MARKER_TERMS)


def has_nepali_result_term(text):
    return contains_any(text, NEPALI_RESULT_TERMS)


def has_nepali_non_result_term(text):
    return contains_any(text, NEPALI_NON_RESULT_TERMS)


def has_nepali_non_csit_marker(text):
    """True when the title names another programme (M.A./M.Sc./PhD/LAW).

    Used only to turn "no CSIT marker" from a shrug into a POSITIVE rejection
    for the notices we have actually observed. Absence is never read as
    absence: the caller may only use a True here, never a False, to reject.
    """
    return contains_any(text, NEPALI_NON_CSIT_MARKER_TERMS)
