#!/usr/bin/env python3
"""tests/render/test_held_back_pools.py — the edition line states each pool.

Run 43246133293 rendered "7 stories were held back because they are not in
English" over a 15-card edition, while the run log said `held back 7` out of 53
CANDIDATES. The number was right, the population was wrong, and nothing on the
page let a reader check it.

The renderer now takes the counts as data (`held_back.count` with its `pool`,
and `held_back.stories_dropped`) and writes one sentence per population. An
older payload without a pool keeps the story-shaped sentence it always had.
"""

import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "scripts")
for _path in (_HERE, _SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import render_digest  # noqa: E402

SLOT, DAY, STAMP = "evening", "2026-11-12", "2026-11-12 18:00"
NO_SCHEDULE = os.path.join(_HERE, "does-not-exist-schedule.json")

CANDIDATES = ("7 of the 53 collected candidates were held back because their "
              "titles are not in English.")
STORIES = "2 stories were held back because they are not in English."


def edition_line(meta):
    html = render_digest.render_page([], SLOT, DAY, STAMP, meta,
                                     schedule_path=NO_SCHEDULE)
    return re.search(r'<p class="health-edition">(.*?)</p>', html).group(1)


class TheRendererNamesItsPopulation(unittest.TestCase):
    def test_a_candidate_count_is_stated_against_candidates(self):
        line = edition_line({"edition_kind": "unsummarised",
                             "held_back": {"count": 7, "pool": 53,
                                           "stories_dropped": 0}})
        self.assertIn(CANDIDATES, line)
        self.assertNotIn("7 stories were held back", line,
                         "the page is quoting a candidate count as a story "
                         "count again, which is the sentence that did not "
                         "balance against 20 stories and 15 cards")

    def test_both_pools_appear_when_both_dropped_something(self):
        line = edition_line({"edition_kind": "unsummarised",
                             "held_back": {"count": 7, "pool": 53,
                                           "stories_dropped": 2}})
        self.assertIn(CANDIDATES, line)
        self.assertIn(STORIES, line)
        self.assertLess(line.index(CANDIDATES), line.index(STORIES),
                        "the candidate pool leads; the edition's own drops "
                        "follow")

    def test_a_payload_without_a_pool_keeps_the_old_sentence(self):
        line = edition_line({"edition_kind": "unsummarised",
                             "held_back": {"count": 6}})
        self.assertIn("6 stories were held back because they are not in "
                      "English.", line)

    def test_an_older_payload_without_the_field_still_reads_its_note(self):
        line = edition_line({"edition_kind": "unsummarised",
                             "notes": "Research note. " + STORIES})
        self.assertIn(STORIES, line)

    def test_zero_stories_dropped_adds_no_sentence(self):
        line = edition_line({"edition_kind": "unsummarised",
                             "held_back": {"count": 7, "pool": 53,
                                           "stories_dropped": 0}})
        self.assertEqual(1, line.count("held back"))


if __name__ == "__main__":
    unittest.main()
