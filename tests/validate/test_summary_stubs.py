#!/usr/bin/env python3
"""tests/validate/test_summary_stubs.py — prose vs stub, and the repair.

Run 42056457121 (2026-11-05) shipped four stories whose summary was
`Reported: <their own headline>`. The deriver never ran (the digest WAS
model-written), the count said they were written, and no surface mentioned it.

These tests pin the three things that close it:
  * `is_real_summary` tells prose from a stub, label and all;
  * `edition_facts` computes the counts on a payload that never had them;
  * `summarise_stubs.repair` replaces a stub, never touches prose, and keeps the
    publisher's text when no model answers.

Offline: the summariser is injected, no provider is contacted.
"""

import unittest

from . import REPO_ROOT  # noqa: F401  (REPO_ROOT puts scripts/ on sys.path)

import summarise_stubs
import validate_stories as vs

TITLE = ("Prepare for delay: TxDOT closing S.A. highway ramp in just hours "
         "- MySA")
SNIPPET = ("Prepare for delay: TxDOT closing S.A. highway ramp in just hours "
           "MySA")
PROSE = ("Reported: TxDOT will close the Loop 1604 ramp at Highway 90 for "
         "resurfacing from 21:00 on Thursday, and drivers heading west are "
         "advised to use the Frontage Road instead.")


def story(url, summary, title=TITLE, snippet=SNIPPET, source=None):
    row = {"title": title, "summary": summary, "source_url": url,
           "category": "local_city"}
    if source:
        row["summary_source"] = source
    return row


class RealSummaryTests(unittest.TestCase):
    def test_a_title_restating_summary_is_a_stub(self):
        self.assertFalse(vs.is_real_summary("Reported: " + TITLE, TITLE, SNIPPET))
        self.assertFalse(vs.is_real_summary(
            "Reported: Prepare for delay: TxDOT closing S.A. highway ramp in "
            "just hours MySA", TITLE, SNIPPET),
            "the summary the run actually shipped was the title without its dash")

    def test_the_snippet_copied_verbatim_is_a_stub(self):
        self.assertFalse(vs.is_real_summary("Reported: " + SNIPPET, TITLE, SNIPPET))
        # A feed description trimmed with an ellipsis is still the publisher's.
        self.assertFalse(vs.is_real_summary(
            SNIPPET[:-8] + "…", TITLE, SNIPPET))

    def test_an_empty_or_short_body_is_a_stub(self):
        for summary in ("", "   ", "Reported:", "Confirmed: just in",
                        "Reported: -"):
            self.assertFalse(vs.is_real_summary(summary, TITLE, SNIPPET),
                             repr(summary))

    def test_real_prose_behind_the_label_is_not_a_stub(self):
        self.assertTrue(vs.is_real_summary(PROSE, TITLE, SNIPPET))

    def test_prose_that_opens_with_the_headline_is_not_a_stub(self):
        text = ("Reported: " + TITLE + ". The closure starts at 21:00 and lasts "
                "until 05:00, so the westbound lanes will be shut overnight.")
        self.assertTrue(vs.is_real_summary(text, TITLE, SNIPPET),
                        "a real summary may open with the headline")

    def test_the_label_alone_does_not_decide_it(self):
        # The prompt REQUIRES the label, so it can never be the stub signal.
        self.assertTrue(vs.is_real_summary(PROSE, TITLE, SNIPPET))
        self.assertFalse(vs.is_real_summary("Reported: " + TITLE, TITLE, SNIPPET))


class CountsOnEveryPathTests(unittest.TestCase):
    def test_a_model_written_payload_that_never_had_the_fields_gets_them(self):
        stories = [story("https://x/1", PROSE)]
        facts = vs.edition_facts({}, stories,
                                 snippets={"https://x/1": SNIPPET})
        self.assertEqual("full", facts["edition_kind"])
        self.assertEqual({"summarised": 1, "total": 1}, facts["summary_counts"])

    def test_a_stub_is_not_counted_as_ai_written(self):
        stories = [story("https://x/1", PROSE),
                   story("https://x/2", "Reported: " + TITLE)]
        facts = vs.edition_facts({}, stories,
                                 snippets={"https://x/1": SNIPPET,
                                           "https://x/2": SNIPPET})
        self.assertEqual("partial", facts["edition_kind"])
        self.assertEqual({"summarised": 1, "total": 2}, facts["summary_counts"])

    def test_publisher_text_is_never_counted_even_when_it_is_prose(self):
        stories = [story("https://x/1", PROSE, source="publisher")]
        facts = vs.edition_facts({}, stories)
        self.assertEqual("unsummarised", facts["edition_kind"])
        self.assertEqual(0, facts["summary_counts"]["summarised"])


class RepairTests(unittest.TestCase):
    def _payload(self, stories):
        return {"stories": stories}

    def test_a_stub_is_replaced_by_the_models_prose(self):
        payload = self._payload([
            story("https://x/1", "Reported: " + TITLE),
            story("https://x/2", PROSE)])
        asked = []

        def summarise(rows, **_kw):
            asked.extend(r["source_url"] for r in rows)
            return {"summaries": {"https://x/1": PROSE}}

        counts = summarise_stubs.repair(
            payload, {"https://x/1": SNIPPET, "https://x/2": SNIPPET},
            summarise=summarise, log=lambda *_a: None)
        self.assertEqual(["https://x/1"], asked,
                         "only the stub is sent to the ladder")
        self.assertEqual(PROSE, payload["stories"][0]["summary"])
        self.assertEqual("ai", payload["stories"][0]["summary_source"])
        self.assertEqual(1, counts["written"])
        self.assertEqual("full", payload["edition_kind"])
        self.assertEqual({"summarised": 2, "total": 2},
                         payload["summary_counts"])

    def test_real_prose_is_never_sent_and_never_rewritten(self):
        payload = self._payload([story("https://x/2", PROSE)])
        called = []
        counts = summarise_stubs.repair(
            payload, {}, summarise=lambda rows, **_kw: called.append(rows) or {},
            log=lambda *_a: None)
        self.assertEqual([], called)
        self.assertEqual(0, counts["stubs"])
        self.assertEqual(PROSE, payload["stories"][0]["summary"])

    def test_a_failed_ladder_keeps_publisher_text_and_a_truthful_count(self):
        payload = self._payload([story("https://x/1", "Reported: " + TITLE)])
        counts = summarise_stubs.repair(
            payload, {"https://x/1": SNIPPET}, summarise=lambda rows, **_kw: {},
            log=lambda *_a: None)
        self.assertEqual(1, counts["publisher"])
        self.assertEqual(SNIPPET, payload["stories"][0]["summary"],
                         "the publisher's own description, quoted verbatim")
        self.assertEqual("publisher", payload["stories"][0]["summary_source"])
        self.assertEqual("unsummarised", payload["edition_kind"])
        self.assertEqual({"summarised": 0, "total": 1},
                         payload["summary_counts"])
        self.assertIn("No AI summary", payload["notes"],
                      "the note has to say it, not stay silent")

    def test_a_story_with_no_snippet_is_left_empty_not_invented(self):
        payload = self._payload([story("https://x/1", "Reported: " + TITLE)])
        summarise_stubs.repair(payload, {}, summarise=lambda rows, **_kw: {},
                               log=lambda *_a: None)
        self.assertEqual("", payload["stories"][0]["summary"],
                         "with nothing to quote, an empty summary is the honest "
                         "choice")

    def test_a_second_repair_does_not_print_the_count_twice(self):
        payload = self._payload([story("https://x/1", "Reported: " + TITLE)])
        summarise_stubs.repair(payload, {"https://x/1": SNIPPET},
                               summarise=lambda rows, **_kw: {}, log=lambda *_a: None)
        once = payload["notes"]
        summarise_stubs.repair(payload, {"https://x/1": SNIPPET},
                               summarise=lambda rows, **_kw: {}, log=lambda *_a: None)
        self.assertEqual(once, payload["notes"],
                         "a rerun prepended the same sentence again: two copies "
                         "of one fact")

    def test_main_repairs_a_derived_edition(self):
        """RUN 42070185644: a derived edition's stubs were never repaired.

        The step returned the moment it saw `writer.rung == derived`, so the
        deriver's own label error had no second stage to catch it.
        """
        import json
        import os
        import tempfile
        from unittest import mock
        payload = {"writer": {"rung": "derived"},
                   "notes": "Fallback edition: an AI wrote the summary for 1 of "
                            "2 stories; the rest quote the publisher's own "
                            "description.",
                   "stories": [story("https://x/1", "Reported: " + TITLE),
                               story("https://x/2", PROSE)]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stories.validated.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            with mock.patch.object(summarise_stubs, "_ladder",
                                   return_value={"summaries":
                                                 {"https://x/1": PROSE}}):
                rc = summarise_stubs.main(
                    ["--in", path, "--candidates", os.path.join(tmp, "absent.json")])
            self.assertEqual(0, rc)
            with open(path, encoding="utf-8") as fh:
                after = json.load(fh)
        self.assertEqual(PROSE, after["stories"][0]["summary"],
                         "the derived edition's stub is repaired")
        self.assertEqual("ai", after["stories"][0]["summary_source"])
        self.assertEqual(PROSE, after["stories"][1]["summary"],
                         "a summary that already passed is never rewritten")
        self.assertEqual({"summarised": 2, "total": 2}, after["summary_counts"])
        self.assertEqual(1, after["notes"].count("an AI wrote the summary"),
                         "the deriver's stale count sentence is replaced, not "
                         "joined by a second one: %r" % after["notes"])
        self.assertIn("an AI wrote the summary for all 2 stories", after["notes"])

    def test_a_derived_repair_reports_its_budget_and_asks_only_for_stubs(self):
        import summarise_stories
        payload = self._payload([story("https://x/1", "Reported: " + TITLE),
                                 story("https://x/2", PROSE)])
        seen = {}

        def summarise(rows, budget_seconds=None):
            seen["urls"] = [r["source_url"] for r in rows]
            seen["budget"] = budget_seconds
            return {"summaries": {"https://x/1": PROSE}}

        summarise_stubs.repair(payload, {"https://x/1": SNIPPET,
                                        "https://x/2": SNIPPET},
                               summarise=summarise, log=lambda *_a: None,
                               derived=True)
        self.assertEqual(["https://x/1"], seen["urls"],
                         "the repair may spend ladder time on the stubs and on "
                         "nothing else")
        self.assertEqual(summarise_stories.TOTAL_BUDGET_SECONDS, seen["budget"],
                         "one edition's whole allowance, so a fifteen-story "
                         "edition cannot hang the job")

    def test_the_most_important_stubs_are_repaired_first(self):
        payload = self._payload([
            story("https://x/1", "Reported: " + TITLE),
            dict(story("https://x/2", "Reported: " + TITLE), priority=9),
            dict(story("https://x/3", "Reported: " + TITLE), priority=5)])
        asked = []
        summarise_stubs.repair(
            payload, {}, summarise=lambda rows, **_kw: asked.extend(
                r["source_url"] for r in rows) or {},
            log=lambda *_a: None, derived=True)
        self.assertEqual(["https://x/2", "https://x/3", "https://x/1"], asked,
                         "when the budget cannot cover every stub, the edition's "
                         "own priority order decides which are attempted")

    def test_a_budget_exhausted_repair_leaves_an_honest_count(self):
        payload = self._payload([
            story("https://x/1", "Reported: " + TITLE),
            story("https://x/2", "Reported: " + TITLE),
            story("https://x/3", "Reported: " + TITLE)])
        summarise_stubs.repair(
            payload, {"https://x/1": SNIPPET, "https://x/2": SNIPPET,
                      "https://x/3": SNIPPET},
            summarise=lambda rows, **_kw: {"summaries": {"https://x/1": PROSE}},
            log=lambda *_a: None, derived=True)
        self.assertEqual({"summarised": 1, "total": 3},
                         payload["summary_counts"],
                         "one of three was written; the other two are not")
        self.assertEqual("partial", payload["edition_kind"])
        self.assertIn("an AI wrote the summary for 1 of 3 stories",
                      payload["notes"])
        self.assertEqual(2, [s.get("summary_source")
                             for s in payload["stories"]].count("publisher"))

    def test_a_model_answer_that_is_still_a_stub_is_not_accepted(self):
        payload = self._payload([story("https://x/1", "Reported: " + TITLE)])
        counts = summarise_stubs.repair(
            payload, {"https://x/1": SNIPPET},
            summarise=lambda rows, **_kw: {"summaries": {
                "https://x/1": "Reported: " + TITLE}},
            log=lambda *_a: None)
        self.assertEqual(0, counts["written"])
        self.assertEqual(1, counts["publisher"])
        self.assertEqual(SNIPPET, payload["stories"][0]["summary"])


#: RUN 42061741326 (2026-11-05): two of the summaries that shipped were the
#: headline rewritten, and every count called them written. Both texts are the
#: run's own, so this calibration cannot drift into a paraphrase of the defect.
ANIME_TITLE = "The Dangers in My Heart Season 3 Set for April 2027"
ANIME = ("Confirmed: A new teaser trailer for the third season of The Dangers "
         "in My Heart has revealed an April 2027 premiere window.")
DEUSI_TITLE = ("Kathmandu District Administration issues Deusi-Bhailo "
               "guidelines")
DEUSI = ("Confirmed: The Kathmandu District Administration has issued 12 "
         "guidelines for the safe and dignified conduct of Deusi-Bhailo "
         "programs this Tihar…")


class TheHeadlineParaphraseIsNotASummary(unittest.TestCase):
    """A body made of the headline's own words is not an account of the story."""

    def test_the_anime_paraphrase_from_the_run_is_a_stub(self):
        self.assertFalse(
            vs.is_real_summary(ANIME, ANIME_TITLE),
            "this shipped as a written summary: it adds 'new teaser trailer' and "
            "'revealed' to the headline and names nothing the headline did not "
            "-- not a date, not a number, not a person, not a place")

    def test_the_deusi_summary_that_carries_the_guideline_count_is_prose(self):
        self.assertTrue(
            vs.is_real_summary(DEUSI, DEUSI_TITLE),
            "it names the issuing body, the count of guidelines and the occasion: "
            "the headline does not carry '12' or 'Tihar'")

    def test_a_paraphrase_that_adds_a_number_is_prose(self):
        title = "Flood closes schools in Chitwan"
        body = ("Reported: The flood has closed schools in Chitwan, and 14 of "
                "them will stay shut for a week while the water recedes.")
        self.assertTrue(vs.is_real_summary(body, title))

    def test_a_paraphrase_that_adds_a_named_place_is_prose(self):
        title = "Road washed away in Sindhupalchok"
        body = ("Reported: The road washed away in Sindhupalchok on Tuesday, and "
                "the Barhabise bridge is closed to all traffic.")
        self.assertTrue(vs.is_real_summary(body, title))

    def test_the_labels_own_words_do_not_count_as_a_detail(self):
        # "Confirmed" is the required label, and "season three" is the headline's
        # own "Season 3" said differently -- neither is new information.
        text = ("Confirmed: Season three of The Dangers in My Heart will "
                "premiere in April 2027, the season the headline describes.")
        self.assertFalse(vs.is_real_summary(text, ANIME_TITLE))

    def test_prose_sharing_only_generic_words_with_the_headline_is_still_prose(self):
        title = "Cabinet approves budget"
        body = ("Reported: Ministers signed off on the spending plan after a "
                "four-hour meeting, with the health ministry receiving the "
                "largest share of the increase.")
        self.assertTrue(vs.is_real_summary(body, title))


if __name__ == "__main__":
    unittest.main()
