"""What an EMPTY edition says about itself, in ONE place.

WHY THIS EXISTS
---------------
Every surface that can render a zero-story edition used to say the same
sentence, unconditionally:

    No items cleared filtering today.

On 2026-10-30 that sentence was a lie. Three real stories were produced and
`validate_stories.py` rejected all three on a `spoiler_reason` schema check, so
nothing "cleared filtering" -- the pipeline ate them. The digest then told the
owner, in calm grey text, that it had been a quiet day.

So the sentence is now a function of the payload's `empty_kind`, which
`validate_stories.py` computes and freezes:

  quiet_day (and no kind at all)  -> the friendly sentence, unchanged. A
                                     genuinely quiet day must still read like
                                     one; paging the owner for a newsless
                                     Tuesday is the failure this avoids.
  broken                          -> the sources returned nothing usable. An
                                     outage, not a filter result.
  validator_rejected_all          -> the stories existed and were all rejected
                                     by the pipeline. A content defect.

`quiet_day` deliberately keeps its old wording: nothing about a quiet day
changed, and a reader who sees the friendly line has been told the truth.

CONSUMERS
---------
`scripts/build_email.py` (email HTML + plain text) and
`scripts/render_digest.py` (website) import this. The ntfy publisher is a
heredoc sliced out of `.github/workflows/daily-news.yml` and exec'd with no
repo on sys.path, so it CANNOT import this module and carries a duplicated copy
of `DEFECT_TEXTS` -- the same documented duplication its NPT label table has.
`tests/digest/test_empty_digest_recap.py` pins the two copies against each
other so they cannot drift silently.
"""

#: Unchanged, and only for a quiet day. The em dash and the trailing full stop
#: are part of the asserted string on several surfaces.
QUIET_DAY_TEXT = "No items cleared filtering today."

#: Every empty kind that is NOT a quiet day, and what it must say instead.
#: Keyed by the value `validate_stories.EMPTY_KINDS` carries, so a new defect
#: kind gets a sentence here or it renders the quiet-day lie.
DEFECT_TEXTS = {
    "broken": ("No story survived \u2014 the news sources returned nothing "
               "usable."),
    "validator_rejected_all": ("No story survived validation \u2014 every story "
                               "produced was rejected by the pipeline."),
}

#: A kind this module has not been taught yet. Never the quiet-day sentence:
#: an unrecognised NON-quiet kind is still evidence of a defect, and failing
#: towards "calm day" is precisely the lie this file exists to stop.
GENERIC_DEFECT_TEXT = ("No story survived \u2014 the digest pipeline reported "
                       "a failure.")


def empty_state_text(empty_kind):
    """The sentence for a zero-story edition of this kind.

    An ABSENT kind falls back to the quiet-day sentence: a payload written
    before `empty_kind` existed must render exactly what it rendered before
    this module did, and that is also what every renderer's fixtures carry. A
    kind that is present but unknown is a defect and says so.
    """
    key = str(empty_kind or "").strip()
    if not key or key == "quiet_day":
        return QUIET_DAY_TEXT
    return DEFECT_TEXTS.get(key, GENERIC_DEFECT_TEXT)
