#!/usr/bin/env python3
"""tests/validate/test_derive_unsummarised.py — the no-AI fallback edition.

Run:
    python -m unittest discover -s tests/validate -t . -v

WHAT IS PINNED HERE
-------------------
The edition a failed model run falls back to is derived from the collector's own
candidates, so every rule below is a rule about what the owner is allowed to see
when no model wrote anything:

  * the collector's freshest-first order is preserved and capped at 3 per
    `domain_category` -- the fallback must not become a 15-story one-category run;
  * `priority` never exceeds 7, because daily-news.yml escalates at `>= 8`;
  * titles under 30 characters, empty categories and near-duplicates
    (Jaccard >= 0.5) are dropped;
  * fewer than 5 stories writes NOTHING, so the existing empty/degraded path
    still owns that case;
  * the written payload carries `edition_kind: unsummarised`, a note that says
    no AI was available, an EMPTY summary and no `for_you`.

The derived story is also fed through `validate_stories.validate_story`, because
the agreed design keeps the validator unchanged: a story the validator drops is
a story this fallback cannot deliver.
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from . import REPO_ROOT, SCRIPTS  # noqa: F401  (SCRIPTS is put on sys.path there)

import derive_unsummarised as dut  # noqa: E402
import validate_stories as vs  # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
FRESH = "2026-10-28T09:00:00+00:00"
OLD = "2026-10-14T09:00:00+00:00"


def cand(title, url, category="world_major", published=OLD, **extra):
    item = {"title": title, "url": url, "source": "feed",
            "domain_category": category, "language": "en",
            "published": published}
    item.update(extra)
    return item


def long_title(seed):
    return "A long enough headline about %s and its consequences today" % seed


# Distinct wording on purpose: near-identical titles ARE duplicates below, so a
# fixture built from one sentence would test the dedupe instead of the cap.
DISTINCT = (
    "Nepal government announces the annual budget for the coming fiscal year",
    "Kathmandu valley reports steady rain through the whole afternoon today",
    "University exam schedule published for the autumn semester students",
    "Local bank raises interest rates on fixed deposits from next month",
    "New anime season confirmed by the studio with a teaser for fans",
)


class DeriveOrderAndCaps(unittest.TestCase):
    def test_first_three_per_category_in_collector_order(self):
        candidates = [cand(DISTINCT[n], "https://a.example/n%d" % n,
                           category="world_major") for n in range(5)]
        candidates.append(cand("Kathmandu council opens a new pedestrian zone",
                               "https://b.example/l1", category="local_city"))
        stories, counts = dut.derive(candidates, now=NOW)
        world = [s for s in stories if s["category"] == "world_major"]
        self.assertEqual(len(world), 3, "the per-category cap is 3, not 5")
        self.assertEqual([s["source_url"] for s in world],
                         ["https://a.example/n0", "https://a.example/n1",
                          "https://a.example/n2"],
                         "the collector's freshest-first order must be preserved")
        self.assertEqual(counts["local_city"], 1)

    def test_priority_never_above_seven(self):
        tier1 = cand(DISTINCT[0], "https://tu.edu.np/n1", published=FRESH)
        tier1_old = cand(DISTINCT[1], "https://tu.edu.np/n2",
                         category="education_tu", published=OLD)
        tier3 = cand(DISTINCT[2], "https://example.org/n1",
                     category="tech_career", published=FRESH)
        tier3_old = cand(DISTINCT[3], "https://example.org/n2",
                         category="bank_local", published=OLD)
        stories, _ = dut.derive([tier1, tier1_old, tier3, tier3_old], now=NOW)
        by_title = {s["title"]: s for s in stories}
        self.assertEqual(by_title[tier1["title"]]["priority"], 7,
                         "6 + 1 fresh must clamp to 7")
        self.assertEqual(by_title[tier1["title"]]["reliability"], "tier2")
        self.assertEqual(by_title[tier1_old["title"]]["priority"], 6)
        self.assertEqual(by_title[tier3["title"]]["priority"], 6)
        self.assertEqual(by_title[tier3_old["title"]]["priority"], 5)
        self.assertEqual(by_title[tier3["title"]]["reliability"], "tier3")
        for story in stories:
            self.assertLessEqual(story["priority"], 7,
                                 "priority >= 8 escalates the push")

    def test_drops_short_titles_and_empty_categories(self):
        stories, _ = dut.derive([
            cand("Too short", "https://a.example/short"),
            cand(long_title("no category"), "https://a.example/nocat",
                 category=""),
            cand(long_title("good one"), "https://a.example/good"),
        ], now=NOW)
        self.assertEqual([s["source_url"] for s in stories],
                         ["https://a.example/good"])

    def test_dedupes_near_duplicate_titles(self):
        first = cand("Nepal government announces the annual budget for next year",
                     "https://a.example/1", category="nepal")
        near = cand("Nepal government announces annual budget for the next year",
                    "https://b.example/2", category="nepal")
        other = cand("Kathmandu valley reports steady rain through the afternoon",
                     "https://c.example/3", category="local_city")
        stories, _ = dut.derive([first, near, other], now=NOW)
        self.assertEqual(len(stories), 2, "Jaccard >= 0.5 titles are duplicates")
        self.assertNotIn("https://b.example/2", [s["source_url"] for s in stories])

    def test_passes_through_language_and_media(self):
        image = {"url": "https://a.example/i.jpg", "type": "image/jpeg"}
        video = {"url": "https://a.example/v.mp4", "type": "video/mp4"}
        stories, _ = dut.derive([
            cand(long_title("media item"), "https://a.example/m", language="ne",
                 image=image, video=video),
        ], now=NOW)
        self.assertEqual(stories[0]["language"], "ne")
        self.assertEqual(stories[0]["image"], image)
        self.assertEqual(stories[0]["video"], video)


class DerivedStorySurvivesTheValidator(unittest.TestCase):
    def test_story_shape_passes_validate_story(self):
        stories, _ = dut.derive([
            cand(long_title("validator shape"), "https://kathmandupost.com/a",
                 category="nepal", published=FRESH),
        ], now=NOW)
        story = stories[0]
        self.assertEqual(story["summary"], "")
        self.assertNotIn("for_you", story)
        self.assertEqual(story["supporting_sources"], [])
        self.assertIsNone(story["personal_relevance"])
        self.assertEqual(story["source_name"], "kathmandupost.com")
        ok, reason = vs.validate_story(story, 0)
        self.assertTrue(ok, "the unchanged validator must accept it: %s" % reason)


class FloorAndPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="derive_floor_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_main(self, candidates):
        path = os.path.join(self.tmp, "candidates.json")
        out = os.path.join(self.tmp, "stories.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"collected_at": "2026-10-28 09:00", "candidates": candidates}, fh)
        rc = dut.main(["--candidates", path, "--out", out, "--today", "2026-10-28",
                       "--slot", "morning"])
        return rc, out

    def test_fewer_than_five_stories_writes_nothing(self):
        candidates = [cand(DISTINCT[n], "https://a.example/%d" % n,
                           category=("world_major" if n < 2 else "local_city"))
                      for n in range(4)]
        rc, out = self.run_main(candidates)
        self.assertEqual(rc, 0, "the floor is not an error, it is a refusal")
        self.assertFalse(os.path.exists(out),
                         "below the floor the empty/degraded path must own the run")

    def test_payload_carries_edition_kind_and_note(self):
        candidates = [cand(DISTINCT[n], "https://a.example/%d" % n,
                           category=("world_major" if n < 3 else "local_city"))
                      for n in range(5)]
        rc, out = self.run_main(candidates)
        self.assertEqual(rc, 0)
        with open(out, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["edition_kind"], "unsummarised")
        self.assertIn("no AI was available", payload["notes"])
        self.assertEqual(payload["date"], "2026-10-28")
        self.assertEqual(payload["digest_time"], "morning")
        self.assertEqual(len(payload["stories"]), 5)
        for story in payload["stories"]:
            self.assertEqual(story["summary"], "")
            self.assertNotIn("for_you", story)

    def test_missing_candidates_file_is_not_a_silent_success(self):
        rc = dut.main(["--candidates", os.path.join(self.tmp, "absent.json"),
                       "--out", os.path.join(self.tmp, "stories.json")])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
