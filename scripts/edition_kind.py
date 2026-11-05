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


#: A MODEL-WRITTEN edition that still has unwritten summaries. "Fallback
#: edition" is the deriver's own name and would be false here -- a model did
#: write most of this page. RUN 42056457121 shipped four stories whose summary
#: restated their own headline and said nothing anywhere; this is the sentence
#: that would have said it.
NOTE_MODEL_SOME = ("AI wrote the summary for %d of %d stories; the rest quote "
                   "the publisher's own description.")
NOTE_MODEL_NONE = ("No AI summary survived for this edition, so the text under "
                   "each headline is the publisher's own description.")


def model_note(kind, written, total):
    """The sentence a model-written edition needs, or None when it needs none."""
    if kind == FULL:
        return None
    if kind == UNSUMMARISED or written <= 0:
        return NOTE_MODEL_NONE
    return NOTE_MODEL_SOME % (written, total)


def has_model_note(notes):
    """True when *notes* already opens with one of the model-written sentences.

    The repair step can run twice (a rerun of the job), and prepending the same
    sentence twice would print one fact as two.
    """
    text = str(notes or "").strip()
    return text.startswith("AI wrote the summary") or \
        text.startswith("No AI summary survived")
