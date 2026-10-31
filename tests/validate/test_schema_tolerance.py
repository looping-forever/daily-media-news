#!/usr/bin/env python3
"""tests/validate/test_schema_tolerance.py — a bad FIELD must not eat a story.

Run:
    python -m unittest discover -s tests/validate -t . -v

WHAT IS PINNED HERE
-------------------
On 2026-10-30 the morning run collected 35 usable candidates, the agent wrote
three real stories, and `validate_stories.py` threw ALL THREE away with

    schema: field 'spoiler_reason' must be a string

`spoiler_reason` had been listed in `REQUIRED_STR_FIELDS`, but the prompt only
demands it for a story that actually spoils something (SPOILER RULES: "For
EVERY gaming/anime/manga_manhwa_manhua story set"). Those three stories had
nothing to spoil, so the field was legitimately absent and the validator
invented a violation. The digest went out empty and the run was filed as a
quiet day.

The rule this file pins, in both directions:

  * DESCRIPTIVE fields are REPAIRED, never fatal. An absent, null or
    wrong-typed `spoiler_reason`, `summary`, `source_name`, `relevance_reason`
    or `language` becomes "" and the story is KEPT. Nothing reads those fields
    to place, rank or link a story, so none of them can justify a drop.
  * EVERY OTHER OPTIONAL FIELD is repaired the same way: a wrong-typed
    `published_at`/`event_date`/`location`/`effective_date`, `event_key`,
    `personal_relevance` or `deadline` becomes absent, and a bad
    `supporting_sources` entry is deleted while the story survives. This is the
    class of defect behind the incident: the same shape hid in seven more
    fields, and each one was a single bad value away from emptying an edition.
  * STRUCTURAL fields stay FATAL. A story with no headline, a non-string
    headline, a blank headline or a non-http(s) `source_url` is not a story and
    is still dropped -- tolerating a bad field is not tolerating a bad story.
    So do `category`, `priority`, `reliability` and `spoiler_level`, and so does
    a `personal_relevance` outside 1-10: an out-of-range rank is a claim the
    payload made and got wrong, not missing information. `tests/fixtures/
    input_relevance_sort.json` depends on that drop.

The repair is REPORTED, not silenced: the caller gets the field names back and
the run summary prints how many were normalised and WHICH fields they were, so
a model that keeps omitting a field is visible in the log instead of invisible
in the payload.

Offline and clock-free: literals and a temp directory only. Stdlib unittest.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from . import REPO_ROOT

import validate_stories as vs  # noqa: E402

FIXTURE_CONFIG = os.path.join(REPO_ROOT, "tests", "fixtures", "config")


def story(**overrides):
    """A story that passes every check, so each test can break ONE thing."""
    base = {
        "title": "Kathmandu Valley Ring Road Widening Begins",
        "summary": "Confirmed: the expansion works started on the eastern section.",
        "category": "nepal",
        "priority": 5,
        "source_name": "Kathmandu Post",
        "source_url": "https://kathmandupost.example.com/ring-road-widening",
        "published_at": "2026-10-30",
        "event_date": None,
        "location": None,
        "reliability": "tier1",
        "spoiler_level": "none",
        "spoiler_reason": "",
        "relevance_reason": "Infrastructure project in the valley.",
        "language": "en",
    }
    base.update(overrides)
    return base


class DescriptiveFieldsAreRepairedTests(unittest.TestCase):
    def test_a_non_spoiler_story_without_spoiler_reason_is_kept(self):
        """THE 2026-10-30 DEFECT, PINNED.

        Fails on the old validator with "schema: missing required field
        'spoiler_reason'" and the story is dropped.
        """
        s = story()
        s.pop("spoiler_reason")

        ok, reason = vs.validate_story(s, 0)

        self.assertTrue(ok, reason)
        self.assertEqual("", s["spoiler_reason"])

    def test_a_null_or_wrong_typed_spoiler_reason_is_normalised(self):
        for bad in (None, 7, ["nothing to spoil"], {"reason": "none"}, False):
            with self.subTest(value=bad):
                s = story(spoiler_reason=bad)
                ok, reason = vs.validate_story(s, 0)
                self.assertTrue(ok, reason)
                self.assertEqual("", s["spoiler_reason"])

    def test_a_spoiler_story_with_no_reason_is_still_kept(self):
        """The field is never a drop reason, not even for a spoiler.

        A missing reason on a story that DOES spoil is a reason to ship it
        without the note, never to throw the headline away.
        """
        s = story(spoiler_level="high")
        s.pop("spoiler_reason")

        ok, reason = vs.validate_story(s, 0)

        self.assertTrue(ok, reason)
        self.assertEqual("", s["spoiler_reason"])

    def test_every_descriptive_field_is_normalised(self):
        s = story()
        for field in ("summary", "source_name", "relevance_reason", "language"):
            s.pop(field)

        ok, reason = vs.validate_story(s, 0)

        self.assertTrue(ok, reason)
        for field in ("summary", "source_name", "relevance_reason", "language"):
            self.assertEqual("", s[field], field)

    def test_the_repair_is_reported_to_the_caller(self):
        s = story()
        s.pop("spoiler_reason")
        repaired = []

        ok, _reason = vs.validate_story(s, 0, repaired)

        self.assertTrue(ok)
        self.assertEqual(["spoiler_reason"], repaired)

    def test_a_clean_story_reports_no_repair(self):
        repaired = []

        ok, reason = vs.validate_story(story(), 0, repaired)

        self.assertTrue(ok, reason)
        self.assertEqual([], repaired)


class EveryOptionalFieldIsRepairedTests(unittest.TestCase):
    """The same rule generalised: no OPTIONAL field may cost a story.

    On 2026-10-31 the model wrote three stories and every one of them was
    thrown away again, this time by a field on this list. Repairing only the
    field named in the last incident fixed the incident, not the class.
    """

    def test_wrong_typed_date_and_location_fields_are_repaired(self):
        cases = (
            ("published_at", 20261030),
            ("event_date", ["2026-10-30"]),
            ("location", {"city": "Kathmandu"}),
            ("effective_date", 1),
        )
        for field, bad in cases:
            with self.subTest(field=field):
                s = story(**{field: bad})
                repaired = []
                ok, reason = vs.validate_story(s, 0, repaired)
                self.assertTrue(ok, reason)
                self.assertIsNone(s[field], field)
                self.assertIn(field, repaired)

    def test_wrong_typed_event_key_is_repaired(self):
        s = story(event_key=["a", "b"])
        repaired = []

        ok, reason = vs.validate_story(s, 0, repaired)

        self.assertTrue(ok, reason)
        self.assertIsNone(s["event_key"])
        self.assertIn("event_key", repaired)

    def test_wrong_typed_supporting_sources_never_cost_the_story(self):
        for bad in ("https://one.example.com/story", {"name": "A"}, 7):
            with self.subTest(value=bad):
                s = story(supporting_sources=bad)
                repaired = []
                ok, reason = vs.validate_story(s, 0, repaired)
                self.assertTrue(ok, reason)
                self.assertNotIn("supporting_sources", s)
                self.assertIn("supporting_sources", repaired)

    def test_a_malformed_supporting_source_entry_is_dropped_not_the_story(self):
        s = story(supporting_sources=[
            {"name": "Kathmandu Post", "url": "https://kathmandupost.example.com/a"},
            {"name": "No URL"},
            {"url": "https://noname.example.com/x"},
            {"name": "FTP", "url": "ftp://files.example.com/y"},
            "https://bare.example.com/z",
            {"name": "Pokhara News", "url": "http://pokhara.example.com/b"},
        ])
        repaired = []

        ok, reason = vs.validate_story(s, 0, repaired)

        self.assertTrue(ok, reason)
        self.assertEqual(
            ["https://kathmandupost.example.com/a", "http://pokhara.example.com/b"],
            [entry["url"] for entry in s["supporting_sources"]])
        self.assertEqual(["supporting_sources"], repaired)

    def test_wrong_typed_personal_relevance_is_repaired(self):
        for bad in ("7", 7.5, True, "high"):
            with self.subTest(value=bad):
                s = story(personal_relevance=bad)
                repaired = []
                ok, reason = vs.validate_story(s, 0, repaired)
                self.assertTrue(ok, reason)
                self.assertIsNone(s["personal_relevance"])
                self.assertIn("personal_relevance", repaired)

    def test_an_out_of_range_personal_relevance_is_still_fatal(self):
        """DELIBERATE, and pinned by tests/fixtures/input_relevance_sort.json."""
        for bad in (0, 11, -3):
            with self.subTest(value=bad):
                ok, reason = vs.validate_story(story(personal_relevance=bad), 0)
                self.assertFalse(ok)
                self.assertIn("out of range 1-10", reason)

    def test_a_malformed_deadline_never_costs_the_story(self):
        for bad in (20261030, "30-10-2026", "2026-13-45", "soon", True):
            with self.subTest(value=bad):
                s = story(deadline=bad)
                repaired = []
                ok, reason = vs.validate_story(s, 0, repaired)
                self.assertTrue(ok, reason)
                self.assertIsNone(s["deadline"])
                self.assertIn("deadline", repaired)

    def test_a_valid_deadline_is_kept(self):
        s = story(deadline="2026-11-15")
        repaired = []

        ok, reason = vs.validate_story(s, 0, repaired)

        self.assertTrue(ok, reason)
        self.assertEqual("2026-11-15", s["deadline"])
        self.assertNotIn("deadline", repaired)

    def test_every_repair_is_reported(self):
        s = story(deadline=20261030, event_key=5, published_at=20261030,
                  supporting_sources="https://one.example.com/story")
        repaired = []

        ok, reason = vs.validate_story(s, 0, repaired)

        self.assertTrue(ok, reason)
        self.assertEqual(["deadline", "event_key", "published_at",
                          "supporting_sources"], sorted(repaired))


class RequiredFieldsStayFatalTests(unittest.TestCase):
    """Tolerance for a FIELD never became tolerance for a STORY."""

    def test_a_wrong_typed_priority_is_still_dropped(self):
        ok, reason = vs.validate_story(story(priority="5"), 0)

        self.assertFalse(ok)
        self.assertIn("priority", reason)

    def test_an_unknown_category_is_still_dropped(self):
        ok, reason = vs.validate_story(story(category="gossip"), 0)

        self.assertFalse(ok)
        self.assertIn("category", reason)

    def test_an_unknown_reliability_is_still_dropped(self):
        ok, reason = vs.validate_story(story(reliability="tier9"), 0)

        self.assertFalse(ok)
        self.assertIn("reliability", reason)

    def test_an_unknown_spoiler_level_is_still_dropped(self):
        ok, reason = vs.validate_story(story(spoiler_level="extreme"), 0)

        self.assertFalse(ok)
        self.assertIn("spoiler_level", reason)


class StructuralFieldsStayFatalTests(unittest.TestCase):
    """The other half of the rule: tolerance for a field, never for a story."""

    def test_a_story_with_no_title_is_dropped(self):
        s = story()
        s.pop("title")

        ok, reason = vs.validate_story(s, 0)

        self.assertFalse(ok)
        self.assertIn("title", reason)

    def test_a_blank_title_is_dropped(self):
        ok, reason = vs.validate_story(story(title="   "), 0)

        self.assertFalse(ok)
        self.assertIn("blank", reason)

    def test_a_non_string_title_is_dropped(self):
        ok, reason = vs.validate_story(story(title=123), 0)

        self.assertFalse(ok)
        self.assertIn("title", reason)

    def test_a_non_http_source_url_is_dropped(self):
        ok, reason = vs.validate_story(
            story(source_url="ftp://wire.example.com/story"), 0)

        self.assertFalse(ok)
        self.assertIn("source_url", reason)

    def test_the_fatal_and_repaired_lists_do_not_overlap(self):
        self.assertEqual(["title"], vs.REQUIRED_STR_FIELDS)
        self.assertEqual(
            [], sorted(set(vs.REQUIRED_STR_FIELDS)
                       & set(vs.NORMALISED_STR_FIELDS)))


class TheIncidentRunTests(unittest.TestCase):
    """The three stories from 2026-10-30, through the real CLI."""

    def _run(self, stories):
        tmp = tempfile.mkdtemp(prefix="schema-tolerance-")
        in_path = os.path.join(tmp, "stories.json")
        out_path = os.path.join(tmp, "stories.validated.json")
        with open(in_path, "w", encoding="utf-8") as fh:
            json.dump({"digest_time": "morning", "date": "2026-10-30",
                       "research_status": "full", "stories": stories}, fh)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vs.main([
                "--in", in_path, "--out", out_path,
                "--history", os.path.join(tmp, "none.json"),
                "--today", "2026-10-30",
                "--config-dir", FIXTURE_CONFIG,
                "--state", os.path.join(tmp, "active_stories.json"),
                "--candidates", os.path.join(tmp, "candidates.json"),
            ])

        payload = {}
        if os.path.isfile(out_path):
            with open(out_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        return rc, payload, buf.getvalue()

    def _incident_stories(self):
        return [
            story(title="Kathmandu Valley Ring Road Widening Begins",
                  source_url="https://kathmandupost.example.com/ring-road"),
            story(title="TU Publishes Revised Exam Schedule",
                  source_url="https://tuexam.example.edu.np/schedule"),
            story(title="New Direct Bus Service to Pokhara Starts",
                  source_url="https://kathmandupost.example.com/bus-pokhara"),
        ]

    def test_three_non_spoiler_stories_survive_and_the_run_is_green(self):
        stories = self._incident_stories()
        for s in stories:
            s.pop("spoiler_reason")

        rc, payload, output = self._run(stories)

        self.assertEqual(0, rc,
                         "the run still reports 'no stories survived' for "
                         "stories whose only flaw is an absent spoiler_reason")
        self.assertEqual(3, len(payload.get("stories") or []))

    def test_the_run_summary_reports_the_repairs(self):
        stories = self._incident_stories()
        for s in stories:
            s.pop("spoiler_reason")

        _rc, _payload, output = self._run(stories)

        self.assertIn("3 field(s) normalised", output,
                      "the repair happened silently, so a model that keeps "
                      "omitting the field would never show up in the log")
        self.assertIn("3 kept, 0 dropped", output)

    def test_the_summary_names_what_was_repaired(self):
        stories = self._incident_stories()
        for s in stories:
            s.pop("spoiler_reason")

        _rc, _payload, output = self._run(stories)

        self.assertIn("(spoiler_reason)", output,
                      "the summary counts the repairs but never names the "
                      "field, so the next incident is invisible again")

    def test_the_summary_separates_raw_from_candidates_and_survivors(self):
        """THE MISREADING THIS LINE EXISTS TO PREVENT.

        Both empty editions were read as "the collector found nothing" (or as
        "a quiet day") while the log actually said three model-written stories
        had been dropped. `candidates`, `raw` and `schema-survivors` must be
        three separate numbers on one line, or the next reader repeats it.
        """
        stories = self._incident_stories()
        for s in stories:
            s.pop("spoiler_reason")

        _rc, _payload, output = self._run(stories)

        self.assertIn(
            "provenance:  candidates=0 raw=3 (model-written stories) "
            "schema-survivors=3 final=3", output)

    def test_a_story_with_nothing_to_render_is_still_dropped(self):
        """Tolerance did not become 'anything goes'."""
        rc, payload, _output = self._run([story(title="")])

        self.assertEqual(2, rc)
        self.assertEqual([], payload.get("stories") or [])


if __name__ == "__main__":
    unittest.main()
