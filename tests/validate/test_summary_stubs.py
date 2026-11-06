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

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from . import REPO_ROOT  # noqa: F401  (REPO_ROOT puts scripts/ on sys.path)

import summarise_stubs
import summarise_stories
import validate_stories as vs

TITLE = ("Prepare for delay: TxDOT closing S.A. highway ramp in just hours "
         "- MySA")
#: RUN 42078530071: this feed description is the headline without its dash. It
#: was quoted as "the publisher's own description", so the card read as a heading
#: with itself underneath.
SNIPPET = ("Prepare for delay: TxDOT closing S.A. highway ramp in just hours "
           "MySA")
#: A real publisher description: names the ramp, the cross-street, a time, and
#: what a driver should do -- none of which the headline says.
DESCRIPTION = ("The Texas Department of Transportation says the Loop 1604 ramp "
               "at Highway 90 closes at 21:00 on Thursday for resurfacing, and "
               "westbound drivers should use the Frontage Road instead.")
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
        self.assertEqual({"summarised": 1, "publisher": 0, "none": 0,
                          "total": 1}, facts["summary_counts"])

    def test_a_stub_is_not_counted_as_ai_written(self):
        stories = [story("https://x/1", PROSE),
                   story("https://x/2", "Reported: " + TITLE)]
        facts = vs.edition_facts({}, stories,
                                 snippets={"https://x/1": SNIPPET,
                                           "https://x/2": SNIPPET})
        self.assertEqual("partial", facts["edition_kind"])
        self.assertEqual({"summarised": 1, "publisher": 0, "none": 1,
                          "total": 2}, facts["summary_counts"])

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
        self.assertEqual({"summarised": 2, "publisher": 0, "none": 0,
                          "total": 2}, payload["summary_counts"])

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
            payload, {"https://x/1": DESCRIPTION},
            summarise=lambda rows, **_kw: {}, log=lambda *_a: None)
        self.assertEqual(1, counts["publisher"])
        self.assertEqual(DESCRIPTION, payload["stories"][0]["summary"],
                         "the publisher's own description, quoted verbatim")
        self.assertEqual("publisher", payload["stories"][0]["summary_source"])
        self.assertEqual("unsummarised", payload["edition_kind"])
        self.assertEqual({"summarised": 0, "publisher": 1, "none": 0,
                          "total": 1}, payload["summary_counts"])
        self.assertIn("No AI summary", payload["notes"],
                      "the note has to say it, not stay silent")

    def test_a_story_with_no_snippet_is_never_left_with_an_empty_body(self):
        """RUN 42074891307: the repair wrote "" and the push printed a heading.

        `lead_text("")` returns "" when the candidate carries no publisher
        description, and the step assigned that empty string over the story's own
        text. Five stories held four, so the empty story is dropped instead.
        """
        stories = [story("https://x/%d" % n, PROSE) for n in range(1, 5)]
        stories.append(story("https://x/5", "Reported: " + TITLE))
        payload = self._payload(stories)
        summarise_stubs.repair(payload, {}, summarise=lambda rows, **_kw: {},
                               log=lambda *_a: None, floor=4)
        self.assertEqual(4, len(payload["stories"]),
                         "the story with no text to print is dropped while the "
                         "floor still holds")
        for row in payload["stories"]:
            self.assertTrue(str(row.get("summary") or "").strip(),
                            "no story may ship an empty body: %r" % (row,))

    def test_a_story_kept_below_the_floor_gets_a_non_empty_body(self):
        payload = self._payload([story("https://x/1", "Reported: " + TITLE),
                                 story("https://x/2", PROSE)])
        summarise_stubs.repair(payload, {}, summarise=lambda rows, **_kw: {},
                               log=lambda *_a: None, floor=4)
        self.assertEqual(2, len(payload["stories"]),
                         "the floor forbids shrinking below itself")
        self.assertEqual("Reported: " + TITLE, payload["stories"][0]["summary"],
                         "it keeps its own previous text, not an empty string")
        self.assertEqual("none", payload["stories"][0]["summary_source"],
                         "its text is not a summary and must never count as one")

    def test_a_story_dropped_for_having_no_summary_respects_the_floor(self):
        five = [story("https://x/%d" % n, PROSE) for n in range(1, 5)]
        five.append(dict(story("https://x/5", ""), title=TITLE))
        payload = self._payload(five)
        summarise_stubs.repair(payload, {}, summarise=lambda rows, **_kw: {},
                               log=lambda *_a: None, floor=4)
        self.assertEqual(4, len(payload["stories"]),
                         "five minus one is the floor, so the drop is allowed")
        for row in payload["stories"]:
            self.assertTrue(str(row.get("summary") or "").strip())

        four = [story("https://x/%d" % n, PROSE) for n in range(1, 4)]
        four.append(dict(story("https://x/4", ""), title=TITLE))
        payload = self._payload(four)
        summarise_stubs.repair(payload, {}, summarise=lambda rows, **_kw: {},
                               log=lambda *_a: None, floor=4)
        self.assertEqual(4, len(payload["stories"]),
                         "four minus one is below the floor, so it is kept")
        self.assertTrue(payload["stories"][-1]["summary"].strip(),
                        "and it still ships a non-empty body")

    def test_a_second_repair_does_not_print_the_count_twice(self):
        payload = self._payload([story("https://x/1", "Reported: " + TITLE)])
        summarise_stubs.repair(payload, {"https://x/1": DESCRIPTION},
                               summarise=lambda rows, **_kw: {}, log=lambda *_a: None)
        once = payload["notes"]
        summarise_stubs.repair(payload, {"https://x/1": DESCRIPTION},
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
        self.assertEqual({"summarised": 2, "publisher": 0, "none": 0,
                          "total": 2}, after["summary_counts"])
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
            payload, {"https://x/1": DESCRIPTION, "https://x/2": DESCRIPTION,
                      "https://x/3": DESCRIPTION},
            summarise=lambda rows, **_kw: {"summaries": {"https://x/1": PROSE}},
            log=lambda *_a: None, derived=True)
        self.assertEqual({"summarised": 1, "publisher": 2, "none": 0,
                          "total": 3}, payload["summary_counts"],
                         "one of three was written; the other two are not")
        self.assertEqual("partial", payload["edition_kind"])
        self.assertIn("an AI wrote the summary for 1 of 3 stories",
                      payload["notes"])
        self.assertEqual(2, [s.get("summary_source")
                             for s in payload["stories"]].count("publisher"))

    def test_a_model_answer_that_is_still_a_stub_is_not_accepted(self):
        payload = self._payload([story("https://x/1", "Reported: " + TITLE)])
        counts = summarise_stubs.repair(
            payload, {"https://x/1": DESCRIPTION},
            summarise=lambda rows, **_kw: {"summaries": {
                "https://x/1": "Reported: " + TITLE}},
            log=lambda *_a: None)
        self.assertEqual(0, counts["written"])
        self.assertEqual(1, counts["publisher"])
        self.assertEqual(DESCRIPTION, payload["stories"][0]["summary"])


class ThePublisherFallbackPassesTheSameTest(unittest.TestCase):
    """RUN 42078530071: 6 of 8 "publisher text" bodies were the headline again.

    The fallback quoted the feed's snippet without asking whether it was prose,
    so a card read as a heading with itself underneath while the note called it
    "the publisher's own description". One predicate decides now.
    """

    def repair(self, stories, snippets, floor=4):
        payload = {"stories": stories}
        counts = summarise_stubs.repair(
            payload, snippets, summarise=lambda rows, **_kw: {},
            log=lambda *_a: None, floor=floor)
        return payload, counts

    def test_a_headline_snippet_is_never_used_as_a_summary(self):
        payload, counts = self.repair(
            [story("https://x/1", "Reported: " + TITLE)],
            {"https://x/1": SNIPPET})
        row = payload["stories"][0]
        self.assertEqual(0, counts["publisher"],
                         "the headline echoed is not the publisher's description")
        self.assertEqual(1, counts["none"])
        self.assertEqual("none", row["summary_source"])
        self.assertTrue(row["summary"].strip(), "and it is still not empty")

    def test_a_headline_paraphrase_snippet_is_not_a_summary(self):
        title = "The Dangers in My Heart Season 3 Set for April 2027"
        paraphrase = ("A new teaser trailer for the third season of The Dangers "
                      "in My Heart has revealed an April 2027 premiere window.")
        payload, counts = self.repair(
            [dict(story("https://x/1", "Reported: " + title), title=title)],
            {"https://x/1": paraphrase})
        self.assertEqual(0, counts["publisher"],
                         "the headline rewritten is not a description")
        self.assertNotEqual(paraphrase, payload["stories"][0]["summary"])

    def test_a_headline_snippet_obeys_the_floor(self):
        stories = [story("https://x/%d" % n, PROSE) for n in range(1, 5)]
        stories.append(story("https://x/5", "Reported: " + TITLE))
        payload, counts = self.repair(
            stories, {"https://x/5": SNIPPET}, floor=4)
        self.assertEqual(1, counts["dropped"],
                         "nothing usable to print, so it leaves the edition")
        self.assertEqual(4, len(payload["stories"]))
        for row in payload["stories"]:
            self.assertNotEqual(SNIPPET, row["summary"])


class BothPromptsDemandADetailTheHeadlineLacks(unittest.TestCase):
    """RUN 42078530071: 21 of 21 model answers were rejected as a paraphrase.

    The prompt asked for a summary and never for a detail the headline lacks, so
    the models did exactly what they were told. These assert the instruction is
    in both prompts, by content, so a reworded rule still passes and a deleted
    one goes red.
    """

    PATTERN = r"number.{0,200}(named person|name).{0,200}(place).{0,200}date"

    def test_the_digest_prompt_carries_the_requirement(self):
        import os
        from . import REPO_ROOT
        path = os.path.join(str(REPO_ROOT), "prompts", "daily-digest.md")
        with open(path, encoding="utf-8") as fh:
            text = " ".join(fh.read().lower().split())
        self.assertRegex(text, self.PATTERN)
        self.assertIn("never restate the headline", text)

    def test_the_summariser_prompt_carries_the_requirement(self):
        import summarise_stories
        low = " ".join(summarise_stories.PROMPT.lower().split())
        self.assertRegex(low, self.PATTERN)
        self.assertIn("restates the headline", low)


class TheNoteMatchesWhatTheStoriesCarry(unittest.TestCase):
    """RUN 42074891307: the note said the rest quote the publisher's description.

    The story under it carried an empty body, so the sentence was false. All four
    combinations are pinned here: some/none written crossed with some/none
    publisher text.
    """

    def repair(self, stories, summarise, snippets, derived=True):
        payload = {"stories": stories}
        summarise_stubs.repair(payload, snippets, summarise=summarise,
                               log=lambda *_a: None, derived=derived, floor=3)
        return payload

    def test_some_written_and_some_publisher(self):
        stories = [story("https://x/1", "Reported: " + TITLE),
                   story("https://x/2", "Reported: " + TITLE),
                   story("https://x/3", "Reported: " + TITLE)]
        payload = self.repair(
            stories,
            lambda rows, **_kw: {"summaries": {"https://x/1": PROSE}},
            {"https://x/1": DESCRIPTION, "https://x/2": DESCRIPTION,
             "https://x/3": DESCRIPTION})
        notes = payload["notes"]
        self.assertIn("an AI wrote the summary for 1 of 3 stories", notes)
        self.assertIn("the rest quote the publisher's own description", notes)
        self.assertNotIn("carry no summary", notes)

    def test_some_written_and_no_publisher_text(self):
        stories = [story("https://x/1", "Reported: " + TITLE),
                   story("https://x/2", "Reported: " + TITLE),
                   story("https://x/3", "Reported: " + TITLE)]
        payload = self.repair(
            stories,
            lambda rows, **_kw: {"summaries": {"https://x/1": PROSE}}, {})
        notes = payload["notes"]
        self.assertIn("an AI wrote the summary for 1 of 3 stories", notes)
        self.assertIn("2 carry no summary", notes)
        self.assertNotIn("publisher's own description", notes,
                         "no story carries publisher text: the note cannot say "
                         "they quote it")

    def test_no_story_written_but_publisher_text_exists(self):
        stories = [story("https://x/1", "Reported: " + TITLE),
                   story("https://x/2", "Reported: " + TITLE),
                   story("https://x/3", "Reported: " + TITLE)]
        payload = self.repair(stories, lambda rows, **_kw: {},
                              {"https://x/1": DESCRIPTION,
                               "https://x/2": DESCRIPTION,
                               "https://x/3": DESCRIPTION})
        notes = payload["notes"]
        self.assertIn("no AI summary survived", notes)
        self.assertIn("publisher's own description", notes)

    def test_no_story_written_and_no_publisher_text(self):
        stories = [story("https://x/1", "Reported: " + TITLE),
                   story("https://x/2", "Reported: " + TITLE),
                   story("https://x/3", "Reported: " + TITLE)]
        payload = self.repair(stories, lambda rows, **_kw: {}, {})
        notes = payload["notes"]
        self.assertIn("no AI summary survived", notes)
        self.assertIn("no story carries a summary of its own", notes)
        self.assertNotIn("publisher's own description", notes,
                         "the empty text is not the publisher's description")


class ARejectedAnswerIsRecorded(unittest.TestCase):
    """FIX 3: "0 written" must be diagnosable, not a silent discard."""

    def test_a_rejected_answer_is_written_to_the_debug_file(self):
        import json
        import os
        import tempfile
        payload = {"stories": [story("https://x/1", "Reported: " + TITLE)]}
        answer = "Reported: " + TITLE
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "summarise-debug.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"attempts": [{"reason": "exit_nonzero"}]}, fh)
            summarise_stubs.repair(
                payload, {"https://x/1": SNIPPET},
                summarise=lambda rows, **_kw: {
                    "summaries": {"https://x/1": answer},
                    "model": "gemini/test-model"},
                log=lambda *_a: None, floor=1, debug_path=path)
            with open(path, encoding="utf-8") as fh:
                written = json.load(fh)
        self.assertEqual([{"reason": "exit_nonzero"}], written["attempts"],
                         "a failed request keeps the record it already had")
        record = written["rejected"][0]
        self.assertEqual(TITLE, record["title"])
        self.assertEqual("gemini/test-model", record["model"])
        self.assertEqual(answer, record["text"],
                         "the verbatim answer, not a summary of it")
        self.assertTrue(record["reason"],
                        "the predicate's own reason, so '0 written' is diagnosable")


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


class TheBudgetIsAWallClock(unittest.TestCase):
    """RUN 42144852087: 01:38:01 -> 01:46:13 against a 300s allowance.

    Fourteen rungs failed, the step was killed by its own 8-minute cap, nothing
    was written and the step still reported success. These tests pin the three
    things that cannot happen again: the allowance is measured from the step's
    own start, a spent allowance still writes the edition and says so once, and
    a raising ladder still writes it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stubs_budget_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "stories.validated.json")
        self.candidates = os.path.join(self.tmp, "candidates.json")
        with open(self.candidates, "w", encoding="utf-8") as fh:
            json.dump({"candidates": []}, fh)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"notes": "",
                       "stories": [story("https://a.example/%d" % n, "",
                                         snippet=SNIPPET)
                                   for n in range(6)]}, fh)

    def run_main(self, ladder):
        out = io.StringIO()
        with mock.patch.object(summarise_stubs, "_ladder", ladder):
            with contextlib.redirect_stdout(out):
                rc = summarise_stubs.main(
                    ["--in", self.path,
                     "--candidates", self.candidates])
        return rc, out.getvalue()

    def test_a_spent_budget_writes_the_edition_and_says_so_once(self):
        rc, out = self.run_main(
            lambda stubs, budget_seconds=None: {
                "summaries": {}, "model": None, "requests": 4,
                "debug": [{}, {}, {}, {}], "budget_spent": True})
        self.assertEqual(0, rc)
        self.assertIn("summarise_stubs: budget spent after 4 rungs, "
                      "0 repaired, 6 left \u2014 writing", out)
        self.assertEqual(1, out.count("budget spent after"),
                         "one verdict line, not one per rung")
        with open(self.path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertTrue(payload["stories"],
                        "the floor still keeps stories in the edition")
        for row in payload["stories"]:
            self.assertTrue(str(row["summary"]).strip(),
                            "a blank body shipped from the repair path")

    def test_the_allowance_is_measured_from_the_steps_own_start(self):
        seen = []

        def note(stubs, budget_seconds=None):
            seen.append(budget_seconds)
            return {"summaries": {}, "requests": 0, "budget_spent": False}

        rc, _ = self.run_main(note)
        self.assertEqual(0, rc)
        self.assertEqual(1, len(seen))
        self.assertLessEqual(seen[0], summarise_stories.TOTAL_BUDGET_SECONDS)
        self.assertGreater(seen[0], 0, "the walk must still get what is left")

    def test_a_raising_ladder_still_writes_the_edition(self):
        def boom(stubs, budget_seconds=None):
            raise RuntimeError("catalogue exploded")

        rc, out = self.run_main(boom)
        self.assertEqual(0, rc, "a broken ladder must not fail the job")
        self.assertIn("the ladder raised", out)
        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertTrue(all(str(row["summary"]).strip()
                            for row in payload["stories"]))

    def test_repair_reports_the_spent_budget_to_its_caller(self):
        counts = summarise_stubs.repair(
            {"notes": "", "stories": [story("https://a.example/1", "",
                                            snippet=SNIPPET)]},
            summarise=lambda stubs, budget_seconds=None: {
                "summaries": {}, "requests": 2, "budget_spent": True},
            log=lambda *_a: None)
        self.assertTrue(counts["budget_spent"])


if __name__ == "__main__":
    unittest.main()
