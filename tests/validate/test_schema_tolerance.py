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
  * STRUCTURAL fields stay FATAL. A story with no headline, a non-string
    headline, a blank headline or a non-http(s) `source_url` is not a story and
    is still dropped -- tolerating a bad field is not tolerating a bad story.

The repair is REPORTED, not silenced: the caller gets the field names back and
the run summary prints how many were normalised, so a model that keeps omitting
a field is visible in the log instead of invisible in the payload.

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

    def test_a_story_with_nothing_to_render_is_still_dropped(self):
        """Tolerance did not become 'anything goes'."""
        rc, payload, _output = self._run([story(title="")])

        self.assertEqual(2, rc)
        self.assertEqual([], payload.get("stories") or [])


if __name__ == "__main__":
    unittest.main()
