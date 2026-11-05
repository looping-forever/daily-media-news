#!/usr/bin/env python3
"""tests/render/test_edition_label.py — "unsummarised edition (no AI today)".

WHY THIS EXISTS. The no-AI fallback renders headlines with an EMPTY summary. An
unlabelled empty summary is indistinguishable from a model that had nothing to
say, so the label is not decoration: it is the only thing that keeps a
degraded edition honest on the page. Three surfaces must say the same words
(page, email subject, push title); this file pins the PAGE and the renderer
contract.

The property that matters as much as the label itself:

    NO edition_kind, NO CHANGE. An absent `edition_kind` renders the page
    byte-for-byte as it did before this feature, exactly like `delivery_line`.

The words are pinned as a literal, not imported from the module under test,
because a test that imports the constant cannot notice the constant being
edited.
"""

import os
import re
import sys
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from fixtures import validated_payload  # noqa: E402

import render_digest  # noqa: E402
import build_email  # noqa: E402
import english_text  # noqa: E402

LABEL = "Unsummarised edition (no AI today)"
SLOT = "evening"
DAY = "2026-10-28"
STAMP = "2026-10-28 18:00"
NO_SCHEDULE = os.path.join(_HERE, "does-not-exist-schedule.json")


class HealthBlock(unittest.TestCase):
    def test_absent_renders_nothing_at_all(self):
        self.assertEqual(render_digest.render_health_block({"state": "ok"}), "",
                         "a caller that never heard of the edition kind gets "
                         "the old bytes: no line, no placeholder")

    def test_label_is_its_own_line_not_an_alarm(self):
        html = render_digest.render_health_block({"state": "ok"},
                                                 edition_line=LABEL)
        self.assertEqual(html, '<p class="health-edition">%s</p>' % LABEL,
                         "a healthy unsummarised page must NOT be wrapped in "
                         "the red .health-banner: no AI is a kind, not a fault")
        self.assertNotIn("health-banner", html)

    def test_label_survives_alongside_a_real_health_alarm(self):
        health = {"state": "degraded",
                  "critical": {"hosts_down": ["tu.edu.np"]}}
        html = render_digest.render_health_block(health, edition_line=LABEL)
        self.assertIn("health-banner", html)
        self.assertIn('<p class="health-edition">%s</p>' % LABEL, html)
        self.assertLess(html.index("health-edition"), html.index("health-down"),
                        "the edition kind leads; the host list follows")

    def test_label_text_is_escaped(self):
        html = render_digest.render_health_block({"state": "ok"},
                                                 edition_line="<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)


class RenderPage(unittest.TestCase):
    def page(self, meta):
        return render_digest.render_page([], SLOT, DAY, STAMP, meta,
                                         schedule_path=NO_SCHEDULE)

    def test_unsummarised_meta_renders_the_label(self):
        html = self.page({"edition_kind": "unsummarised"})
        self.assertIn(LABEL, html)
        self.assertIn('<p class="health-edition">', html)

    def test_ordinary_meta_renders_no_label(self):
        for meta in ({}, {"edition_kind": None}, {"edition_kind": "full"},
                     validated_payload("ok")):
            html = self.page(meta)
            self.assertNotIn(LABEL, html)
            self.assertNotIn('<p class="health-edition">', html,
                             "no edition kind means no new ELEMENT (the CSS rule "
                             "is always in the stylesheet)")

    def test_only_the_exact_kind_is_labelled(self):
        for kind in ("", "partial", "UNSUMMARISED", "unsummarised-ish"):
            self.assertNotIn(LABEL, self.page({"edition_kind": kind}))


class TheLabelSaysHowShortTheEditionIs(unittest.TestCase):
    """A label that only says "no AI" explains empty summaries, not a short page.

    On 2026-11-03 the page carried one story fewer because its headline was not in
    English. The count lives in `notes`, which are CAPPED for display and lose the
    sentence first, so the reader-visible edition line is where it belongs.
    """

    COUNTED = "2 stories were held back because they are not in English."

    def page(self, notes):
        meta = {"edition_kind": "unsummarised", "notes": notes}
        return render_digest.render_page([], SLOT, DAY, STAMP, meta,
                                         schedule_path=NO_SCHEDULE)

    def test_the_count_rides_on_the_edition_line(self):
        html = self.page("Research note. " + self.COUNTED)
        self.assertIn(LABEL + " - " + self.COUNTED, html,
                      "the label states the kind and the count in one line")

    def test_a_note_without_a_count_leaves_the_label_alone(self):
        html = self.page("Research note: two sources were unreachable.")
        self.assertIn('<p class="health-edition">%s</p>' % LABEL, html)
        self.assertNotIn("held back", html)


class TheLabelCountsWhatTheModelWrote(unittest.TestCase):
    """A fallback whose stories DO carry written summaries cannot say "no AI".

    2026-11-05 shipped fifteen headlines over publisher descriptions. The fix is
    a batch of small model requests, and the moment some of those requests land
    the page has two kinds of text under one headline style. `edition_kind` still
    says `unsummarised` (no model wrote the DIGEST, and the seal must keep
    treating it as degraded); the label is where the count belongs.
    """

    PART = ("Fallback edition: AI summary for 4 of 15 stories, the rest quote "
            "the publisher")
    ALL = "Fallback edition: AI summary for all 15 stories"

    def page(self, meta):
        return render_digest.render_page([], SLOT, DAY, STAMP, meta,
                                         schedule_path=NO_SCHEDULE)

    def test_a_partial_fallback_counts_both_kinds(self):
        html = self.page({"edition_kind": "unsummarised",
                          "summary_counts": {"summarised": 4, "total": 15}})
        self.assertIn(self.PART, html)
        self.assertNotIn(LABEL, html, "the old label over written summaries is "
                                      "the false claim this change removes")

    def test_a_fully_summarised_fallback_says_so_without_a_fraction(self):
        html = self.page({"edition_kind": "unsummarised",
                          "summary_counts": {"summarised": 15, "total": 15}})
        self.assertIn(self.ALL, html)

    def test_zero_summaries_keeps_the_old_label(self):
        html = self.page({"edition_kind": "unsummarised",
                          "summary_counts": {"summarised": 0, "total": 15}})
        self.assertIn('<p class="health-edition">%s</p>' % LABEL, html)

    def test_counts_without_the_kind_render_nothing(self):
        html = self.page({"summary_counts": {"summarised": 4, "total": 15}})
        self.assertNotIn("Fallback edition", html)
        self.assertNotIn('<p class="health-edition">', html)

    def test_malformed_counts_do_not_break_the_old_label(self):
        for counts in (None, {}, {"summarised": "4", "total": 15},
                       {"summarised": 4}, "4 of 15"):
            html = self.page({"edition_kind": "unsummarised",
                              "summary_counts": counts})
            self.assertIn('<p class="health-edition">%s</p>' % LABEL, html,
                          "counts %r must fall back to the old label rather "
                          "than render a wrong one" % (counts,))


class TheWordsAreTheSameOnAllThreeSurfaces(unittest.TestCase):
    """ONE EDITION, THREE SURFACES, ONE SENTENCE.

    The page, the email subject and the push title are written in two different
    files by two different mechanisms, and nothing makes them agree. Reword one
    and an edition arrives on the phone described differently from the page it
    links to -- which is the class of defect that produced the label in the first
    place: a claim and the evidence disagreeing.

    The emails and the push are read out of the workflow as TEXT, deliberately:
    the alternative is a test that imports the thing it is checking, which
    cannot notice the thing being edited.
    """

    PHRASE = "unsummarised edition (no ai today)"

    def setUp(self):
        self.workflow = (Path(__file__).resolve().parents[2] / ".github" /
                         "workflows" / "daily-news.yml").read_text(encoding="utf-8")

    def test_the_page_uses_the_agreed_words(self):
        self.assertEqual(LABEL.lower(), self.PHRASE,
                         "the page's label changed. Every other surface is "
                         "pinned to these words.")

    def test_the_push_title_uses_the_agreed_words(self):
        self.assertRegex(self.workflow, re.compile(re.escape(self.PHRASE),
                                                   re.IGNORECASE),
                         "the push title no longer carries %r" % self.PHRASE)

    def test_the_email_subject_folds_the_tag_into_the_subject(self):
        """The tag has to REACH the subject, not merely exist beside it.

        This used to pin the identifier `edition_tag` by name. That was the wrong
        thing to hold: renaming a local variable leaves the label untouched, and
        the assertion would fail on a correct workflow. Worse, it would have
        passed a broken one -- nothing checked that the variable was ever
        interpolated into the subject, so a subject built without the tag read as
        green. So the name is CAPTURED rather than required, and the claim is the
        composition itself: whatever holds the tag is interpolated into the
        subject line, and that subject is what the send step reads.
        """
        block = self._subject_block()
        assigned = re.search(
            r"(\w+)\s*=\s*' - " + re.escape(self.PHRASE) + r"'", block,
            re.IGNORECASE)
        self.assertIsNotNone(
            assigned,
            "the unsummarised tag is no longer assigned in the build-email step, "
            "so the subject has nothing to append. Block checked:\n%s" % block)
        name = assigned.group(1)
        self.assertRegex(
            block,
            re.compile(r"subject\s*=\s*'Daily Media News - %s - %s%s'\s*%\s*"
                       r"\([^)]*\b" + re.escape(name) + r"\b"),
            "the subject is composed without the tag variable %r, so an "
            "unsummarised edition reaches the owner's lock screen labelled as a "
            "normal digest. Block checked:\n%s" % (name, block))
        self.assertIn("state/email_subject.txt", block,
                      "the composed subject is no longer written to the file the "
                      "send step reads")

    def _subject_block(self):
        """The build-email step's subject heredoc, as text."""
        write_at = self.workflow.index("state/email_subject.txt', 'w'")
        start = self.workflow.rindex("python3 - <<'PY'", 0, write_at)
        end = re.compile(r"\n\s*PY\b").search(self.workflow, start).start()
        return self.workflow[start:end]

    def test_no_surface_has_silently_reworded_it(self):
        variants = {m.lower() for m in
                    re.findall(r"unsummarised edition \(no AI [^)]*\)",
                               self.workflow, re.IGNORECASE)}
        self.assertEqual({self.PHRASE}, variants,
                         "the workflow describes the fallback in more than one "
                         "way: %r. One edition, one sentence." % (variants,))
        self.assertGreaterEqual(
            len(re.findall(re.escape(self.PHRASE), self.workflow, re.IGNORECASE)),
            2,
            "only one place in the workflow names the edition, so either the "
            "email or the push lost its label")


class TheCountIsCarriedOnceNotReread(unittest.TestCase):
    """Run 41995372308 put two held-back counts in front of one reader.

    The page's edition line said six, the research note said five. Both numbers were
    read out of the same prose field by different readers -- the line with a regex,
    the note by rendering the text itself -- and the count had been written there by
    two different stages. A number that lives in a sentence can be re-derived
    differently; a number that lives on the payload cannot.
    """

    SENTENCE = "5 stories were held back because they are not in English."

    def page(self, meta):
        return render_digest.render_page([], SLOT, DAY, STAMP, meta,
                                         schedule_path=NO_SCHEDULE)

    def test_the_carried_count_beats_the_one_in_the_notes(self):
        html = self.page({"edition_kind": "unsummarised",
                          "notes": self.SENTENCE,
                          "held_back": {"count": 6, "reason": "not_english"}})
        self.assertIn(LABEL + " - 6 stories were held back", html)
        self.assertNotIn(LABEL + " - 5 stories", html,
                         "the renderer read the number back out of the note instead "
                         "of the field both surfaces are given")

    def test_a_carried_zero_says_nothing_even_when_the_note_claims_a_count(self):
        html = self.page({"edition_kind": "unsummarised",
                          "notes": self.SENTENCE,
                          "held_back": {"count": 0, "reason": "not_english"}})
        line = re.search(r'<p class="health-edition">(.*?)</p>', html).group(1)
        self.assertEqual(LABEL, line,
                         "zero is a fact, not a missing value: the old payloads "
                         "without the field are the ones that read the note")

    def test_a_payload_without_the_field_still_reads_the_note(self):
        html = self.page({"edition_kind": "unsummarised", "notes": self.SENTENCE})
        self.assertIn(LABEL + " - 5 stories were held back", html,
                      "an edition rendered from an older payload must keep its count")

    def test_the_page_and_the_email_print_the_same_number(self):
        count = 6
        notes = english_text.replace_held_back("Research note.", count)
        meta = {"edition_kind": "unsummarised", "notes": notes,
                "held_back": {"count": count, "reason": "not_english"}}
        page = self.page(meta)
        mail = build_email.notice_text({}, notes)
        numbers = set(int(n) for n in re.findall(
            r"(\d+) stor(?:y was|ies were) held back", page + mail))
        self.assertEqual({count}, numbers,
                         "the page and the email disagree about how many stories "
                         "the edition held back: %r" % (numbers,))
        self.assertEqual(2, len(re.findall(r"held back", page)),
                         "the sentence must reach the page exactly twice: once on "
                         "the edition line, once in the note")


if __name__ == "__main__":
    unittest.main()
