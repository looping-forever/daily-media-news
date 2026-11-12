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

#: A card with no text at all renders as a headline and a bare `Link:`. This is
#: what goes in its place -- still counted `none`, because nobody wrote it.
NO_SUMMARY_TEXT = "No summary available."


def _carry(n):
    return "carries no summary" if n == 1 else "carry no summary"


#: A card whose body is single sentences copied verbatim from the publisher's
#: own text by the deterministic extractor. It is neither AI-written nor the
#: feed's description quoted whole, so it gets its own count and its own
#: half-sentence: the page must show how many cards were extracted, not written.
NOTE_EXTRACTED = (" %d of them are single sentences picked out of the "
                  "publisher's text.")


def extracted_clause(extracted):
    """`" 3 of them are single sentences picked ..."`, or "" when there are none."""
    try:
        count = int(extracted or 0)
    except (TypeError, ValueError):
        return ""
    return NOTE_EXTRACTED % count if count > 0 else ""


def coverage_phrase(publisher, none, total=None, extracted=0):
    """Who supplied the text for the stories no model wrote -- and how many have none.

    RUN 42074891307: the note said "the rest quote the publisher's own
    description" over a story with no publisher text at all. A story with neither
    a written summary nor publisher text is stated as such, never folded into the
    publisher claim. All four combinations resolve here, so no caller has to
    guess which half is true.

    `extracted` names the subset of the publisher-side cards whose text was
    picked out sentence by sentence rather than quoted whole. It defaults to 0,
    which keeps every sentence written before that count existed byte-identical.
    """
    if publisher <= 0:
        if total is not None and none >= total:
            return "no story carries a summary of its own" \
                + extracted_clause(extracted)
        return "%d %s" % (none, _carry(none)) + extracted_clause(extracted)
    if none <= 0:
        return "the rest quote the publisher's own description" \
            + extracted_clause(extracted)
    return ("%d quote the publisher's own description and %d %s"
            % (publisher, none, _carry(none))) + extracted_clause(extracted)


def sourcing_note(kind, written, total, publisher, none, fallback=True,
                  extracted=0):
    """The count sentence for all four combinations of written/publisher text."""
    if written <= 0:
        if publisher <= 0:
            return (NOTE_NO_TEXT_FALLBACK if fallback else NOTE_NO_TEXT_MODEL) \
                + extracted_clause(extracted)
        if none <= 0:
            return (NOTE_NONE_SURVIVED if fallback else NOTE_MODEL_NONE) \
                + extracted_clause(extracted)
        pattern = NOTE_REST_FALLBACK if fallback else NOTE_REST_MODEL
        # BOTH sentences carry two counts AND the coverage phrase. This branch
        # passed only the phrase, so a model-written edition with 0 written,
        # some publisher text and some stories with neither raised
        # "TypeError: %d format: a real number is required" inside the repair's
        # own note -- the one path whose whole job is to say why it repaired
        # nothing.
        return pattern % (written, total,
                          coverage_phrase(publisher, none, total, extracted))
    if kind == FULL:
        return NOTE_ALL % total
    if none <= 0:
        return NOTE_SOME % (written, total) + extracted_clause(extracted)
    pattern = NOTE_REST_FALLBACK if fallback else NOTE_REST_MODEL
    return pattern % (written, total,
                      coverage_phrase(publisher, none, total, extracted))


def summary_note(kind, written, total, publisher=None, none=None, extracted=0):
    """The one sentence a fallback edition prints about who wrote its summaries."""
    if written <= 0:
        # Zero is the deriver's ground: it knows WHY no model answered, and that
        # paragraph is a better sentence than anything this function can build.
        return None
    if publisher is None or none is None:
        # A caller that has only the old pair (the deriver's payload) gets the
        # sentence that pair can prove: the rest is all it knows.
        if kind == FULL:
            return NOTE_ALL % total
        return NOTE_SOME % (written, total) + extracted_clause(extracted)
    return sourcing_note(kind, written, total, publisher, none, fallback=True,
                         extracted=extracted)


#: A MODEL-WRITTEN edition that still has unwritten summaries. "Fallback
#: edition" is the deriver's own name and would be false here -- a model did
#: write most of this page. RUN 42056457121 shipped four stories whose summary
#: restated their own headline and said nothing anywhere; this is the sentence
#: that would have said it.
NOTE_MODEL_SOME = ("AI wrote the summary for %d of %d stories; the rest quote "
                   "the publisher's own description.")
NOTE_MODEL_NONE = ("No AI summary survived for this edition, so the text under "
                   "each headline is the publisher's own description.")
#: The combinations the two sentences above cannot state: a story that carries
#: NEITHER a written summary NOR the publisher's description. Each names the
#: count, so an empty body is never described as publisher text.
NOTE_REST_FALLBACK = ("Fallback edition: an AI wrote the summary for %d of %d "
                      "stories; %s.")
NOTE_REST_MODEL = "AI wrote the summary for %d of %d stories; %s."
NOTE_NO_TEXT_FALLBACK = ("Fallback edition: no AI summary survived for this "
                         "edition, and no story carries a summary of its own.")
NOTE_NO_TEXT_MODEL = ("No AI summary survived for this edition, and no story "
                      "carries a summary of its own.")


def model_note(kind, written, total, publisher=None, none=None, extracted=0):
    """The sentence a model-written edition needs, or None when it needs none."""
    if kind == FULL:
        return None
    if publisher is None or none is None:
        if kind == UNSUMMARISED or written <= 0:
            return NOTE_MODEL_NONE + extracted_clause(extracted)
        return NOTE_MODEL_SOME % (written, total) + extracted_clause(extracted)
    return sourcing_note(kind, written, total, publisher, none, fallback=False,
                         extracted=extracted)


def has_model_note(notes):
    """True when *notes* already opens with one of the model-written sentences.

    The repair step can run twice (a rerun of the job), and prepending the same
    sentence twice would print one fact as two.
    """
    text = str(notes or "").strip()
    return text.startswith("AI wrote the summary") or \
        text.startswith("No AI summary survived")
