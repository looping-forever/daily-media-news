"""The edition's own name, derived from the one pair of counters every surface prints.

The word and the number are read from the same two values -- how many stories got
a written summary, and how many stories there are -- so a page that says "22 of
29" can never call itself "unsummarised".
"""

UNSUMMARISED = "unsummarised"
PARTIAL = "partial"
FULL = "full"
KINDS = (UNSUMMARISED, PARTIAL, FULL)


def kind_for(written, total):
    """Which of the three kinds these two numbers describe."""
    if total <= 0 or written <= 0:
        return UNSUMMARISED
    if written >= total:
        return FULL
    return PARTIAL


NOTE_SOME = ("Fallback edition: an AI wrote the summary for %d of %d stories; "
             "the rest quote the publisher's own description.")
NOTE_ALL = ("Fallback edition: an AI wrote the summary for all %d stories; "
            "nothing here is the publisher's own text.")
#: A drop at validation can take away every written summary, and the sentence
#: that said "22 of 29" is then describing an edition that no longer exists.
NOTE_NONE_SURVIVED = ("Fallback edition: no AI summary survived validation for "
                      "this edition, so the text under each headline is the "
                      "publisher's own description.")
#: The phrase both count sentences share, so a stale one can be recognised.
NOTE_COUNT_MARKER = "an AI wrote the summary"


def summary_note(kind, written, total):
    """The one sentence a fallback edition prints about who wrote its summaries."""
    if written <= 0:
        # Zero is the deriver's ground: it knows WHY no model answered, and that
        # paragraph is a better sentence than anything this function can build.
        return None
    if kind == FULL:
        return NOTE_ALL % total
    return NOTE_SOME % (written, total)
