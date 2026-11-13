#!/usr/bin/env python3
"""tests/validate/test_same_event_dedup.py — one event, one card.

WHAT THIS PINS
--------------
Run 43246133293 shipped the SAME Nvidia resignation twice:

  "Nvidia CEO Jensen Huang resigns from leadership positions amid controversy
   over alleged affairs"                                    english.onlinekhabar.com
  "After days of controversy, Nvidia CEO Jensen Huang has resigned"   eurogamer.net

Both carried their own event id, so the event-level check saw two events. Title
Jaccard saw nothing either, and the arithmetic says why: 4 shared tokens out of
15 total is 0.27 against a 0.7 threshold. Lowering the threshold to 0.27 would
merge stories that merely share a subject, so the rule has to look at WHICH
words are shared.

The rule pinned here: the collector's containment test first, then the same cast
of names plus at least SAME_EVENT_OVERLAP of the shorter headline's content
words. The controls are the point of the test — a denial of the same rumour
names the same four entities and must still get its own card, because it is a
different claim, not a different wording.

Offline: temp directories, literals, no provider, stdlib unittest.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from . import REPO_ROOT  # noqa: F401  (REPO_ROOT puts scripts/ on sys.path)

import validate_stories as vs

FIXTURE_CONFIG = os.path.join(REPO_ROOT, "tests", "fixtures", "config")
TODAY = "2026-11-12"

#: The pair from the run. Titles verbatim; the summaries are ordinary prose so
#: nothing here is dropped by a different check than the one under test.
SHOUT = "Nvidia CEO Jensen Huang resigns from leadership positions amid controversy over alleged affairs"
RESTATED = "After days of controversy, Nvidia CEO Jensen Huang has resigned"
DENIAL = "Nvidia CEO Jensen Huang denies reports he is stepping down"
EARNINGS = "Nvidia shares slide as AI chip demand cools"


def story(title, url, source_name, summary, published_at, priority=5):
    """A story that passes every check, so each test varies ONE thing."""
    return {
        "title": title,
        "summary": summary,
        "category": "nepal",
        "priority": priority,
        "source_name": source_name,
        "source_url": url,
        "published_at": published_at,
        "event_date": None,
        "location": None,
        "reliability": "tier1",
        "spoiler_level": "none",
        "spoiler_reason": "",
        "relevance_reason": "Technology story with international reach.",
        "language": "en",
    }


def onlinekhabar(**overrides):
    base = story(
        SHOUT, "https://english.onlinekhabar.com/nvidia-ceo-resigns",
        "Onlinekhabar English",
        "The company confirmed the departure in a filing and named an interim "
        "replacement for the leadership positions he held.",
        "2026-11-12T06:30:00+00:00")
    base.update(overrides)
    return base


def eurogamer(**overrides):
    base = story(
        RESTATED, "https://www.eurogamer.net/nvidia-ceo-has-resigned",
        "Eurogamer",
        "The announcement followed a week of reporting about the chief "
        "executive's personal conduct and the board's response.",
        "2026-11-12T11:05:00+00:00", priority=4)
    base.update(overrides)
    return base


def run_validate(stories):
    tmp = tempfile.mkdtemp(prefix="same-event-")
    in_path = os.path.join(tmp, "stories.json")
    out_path = os.path.join(tmp, "validated.json")
    with open(in_path, "w", encoding="utf-8") as fh:
        json.dump({"digest_time": "morning", "date": TODAY,
                   "research_status": "full", "stories": stories}, fh)
    argv = ["--in", in_path, "--out", out_path,
            "--history", os.path.join(tmp, "history.json"),
            "--today", TODAY,
            "--config-dir", FIXTURE_CONFIG,
            "--state", os.path.join(tmp, "active_stories.json"),
            "--candidates", os.path.join(tmp, "candidates.json")]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = vs.main(argv)
    with open(out_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    return rc, payload


def reasons(payload):
    return [entry["reason"] for entry in payload.get("filter_log") or []]


class TheRuleSeesOneStoryThroughARewrite(unittest.TestCase):
    def test_the_real_pair_is_one_event(self):
        self.assertTrue(vs.same_event(SHOUT, RESTATED),
                        "the pair that shipped twice is not recognised as one "
                        "story, so the edition will carry both again")

    def test_the_order_of_the_two_titles_does_not_matter(self):
        self.assertTrue(vs.same_event(RESTATED, SHOUT))

    def test_title_overlap_alone_could_never_see_it(self):
        """THE STRUCTURAL REASON, measured: 4 of 15 tokens, against 0.7."""
        jac = vs.jaccard(vs.title_tokens(SHOUT), vs.title_tokens(RESTATED))
        self.assertLess(jac, vs.DEDUP_OVERLAP_THRESHOLD,
                        "the pair now trips the old threshold, so this test no "
                        "longer proves the new arm is what catches it")
        self.assertAlmostEqual(0.267, jac, places=3)

    def test_the_shared_words_are_the_names_plus_the_claim(self):
        shared = vs.title_tokens(SHOUT) & vs.title_tokens(RESTATED)
        self.assertEqual({"nvidia", "jensen", "huang", "controversy"},
                         set(shared))

    def test_a_denial_of_the_same_rumour_is_a_different_story(self):
        """The control that matters: same four names, opposite claim."""
        self.assertFalse(vs.same_event(SHOUT, DENIAL),
                         "a denial merged into the report it denies would "
                         "delete a real story and keep the wrong claim")

    def test_one_shared_company_name_is_not_an_event(self):
        self.assertFalse(vs.same_event(SHOUT, EARNINGS))


class TheEditionCarriesOneCard(unittest.TestCase):
    def test_the_pair_ships_once(self):
        rc, payload = run_validate([onlinekhabar(), eurogamer()])
        self.assertEqual(0, rc)
        self.assertEqual([SHOUT], [s["title"] for s in payload["stories"]],
                         "both rewrites are on the page again")
        self.assertTrue(any(r.startswith("duplicate_in_run: same event")
                            for r in reasons(payload)),
                        "the drop is not recorded, so nobody can tell whether "
                        "the second card was deduplicated or lost: %r"
                        % (reasons(payload),))

    def test_the_source_that_reported_first_keeps_the_card(self):
        _rc, payload = run_validate([onlinekhabar(), eurogamer()])
        self.assertEqual("Onlinekhabar English",
                         payload["stories"][0]["source_name"])

    def test_a_better_source_later_in_the_list_takes_the_card(self):
        """Reversed input: the earlier report arrives second and must win.

        Without the eviction arm the first story seen keeps the card, so the
        survivor would depend on model ordering rather than on the report.
        """
        _rc, payload = run_validate([eurogamer(), onlinekhabar()])
        self.assertEqual([SHOUT], [s["title"] for s in payload["stories"]])
        self.assertEqual("Onlinekhabar English",
                         payload["stories"][0]["source_name"])
        self.assertTrue(any("better source kept" in r for r in reasons(payload)),
                        "the earlier report did not replace the later one: %r"
                        % (reasons(payload),))

    def test_an_unresolved_google_redirect_loses_to_the_publisher(self):
        """The only aggregator signal the payload still carries."""
        aggregator = story(
            SHOUT, "https://news.google.com/rss/articles/CBMiV25d",
            "Google News",
            "The company confirmed the departure in a filing and named an "
            "interim replacement for the leadership positions he held.",
            "2026-11-12T06:00:00+00:00")
        _rc, payload = run_validate([aggregator, eurogamer()])
        self.assertEqual(["Eurogamer"],
                         [s["source_name"] for s in payload["stories"]],
                         "a redirect index kept the card over the publisher "
                         "that wrote the story")

    def test_the_denial_still_gets_its_own_card(self):
        denial = story(
            DENIAL, "https://english.onlinekhabar.com/nvidia-ceo-denies",
            "Onlinekhabar English",
            "The chief executive called the reports inaccurate and said he "
            "would not comment further while the board reviews them.",
            "2026-11-12T12:10:00+00:00")
        _rc, payload = run_validate([onlinekhabar(), denial])
        self.assertEqual({SHOUT, DENIAL},
                         {s["title"] for s in payload["stories"]},
                         "the denial was deduplicated away, which hides a "
                         "denial behind the claim it contradicts")


if __name__ == "__main__":
    unittest.main()
