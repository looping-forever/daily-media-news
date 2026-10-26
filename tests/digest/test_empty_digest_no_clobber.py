#!/usr/bin/env python3
"""tests/digest/test_empty_digest_no_clobber.py - an empty build may not erase a page.

WHAT THIS GUARDS. On 2026-10-26 the 20:43 run produced zero stories and wrote
docs/digests/2026-10-26-evening.html anyway, replacing the 13:35 page (3
stories, 20735 bytes) with a 0-story page at the stable slot URL. The deploy
step is gated on EMPTY_DIGEST, but the COMMIT that writes docs/digests/ and the
`cp -r docs/digests` stage are not, so the next successful run republished the
empty page.

WHY THE GUARD IS IN THE WRITER. Every caller that writes this directory has the
same defect, and a bash `if` inside one workflow step cannot be run as a test
that compares real bytes. So render_digest.main() refuses to write a 0-story
page over a page that already holds story cards, and exits non-zero so the
caller's date-comparison swap step is skipped too.

The three cases below are deliberate: refusing is only correct if a page with
stories exists. A first build of a slot, and a retry over an already-empty
page, must still write normally or the retry ladder could never make progress.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import render_digest  # noqa: E402

SLOT_DATE = "2026-10-26"
SLOT = "evening"


def story(n):
    """The fields render_page reads to emit one card."""
    return {"title": "Story %d" % n,
            "summary": "Body for story %d." % n,
            "category": "nepal",
            "published_at": SLOT_DATE}


def existing_page(story_count):
    """A real rendered page, so the fixture is the bytes the writer produces."""
    return render_digest.render_page([story(n) for n in range(story_count)],
                                     SLOT, SLOT_DATE,
                                     "%s 13:35" % SLOT_DATE, {})


class EmptyBuildNoClobber(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="no-clobber-")
        self.out_dir = os.path.join(self.tmp, "digests")
        os.makedirs(self.out_dir)
        self.page = os.path.join(self.out_dir, "%s-%s.html" % (SLOT_DATE, SLOT))
        self.stories_json = os.path.join(self.tmp, "stories.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_render(self, stories):
        with io.open(self.stories_json, "w", encoding="utf-8") as handle:
            json.dump({"stories": stories}, handle)
        argv = ["--digest-time", SLOT, "--date", SLOT_DATE,
                "--email-txt", os.path.join(self.tmp, "absent.txt"),
                "--stories-json", self.stories_json,
                "--candidates", os.path.join(self.tmp, "absent.json"),
                "--out-dir", self.out_dir,
                "--index", os.path.join(self.tmp, "index.html")]
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            rc = render_digest.main(argv)
        return rc, captured.getvalue()

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def test_an_empty_run_leaves_an_existing_non_empty_page_byte_identical(self):
        """THE REGRESSION. This is the 20:43 run against the 13:35 page."""
        with io.open(self.page, "w", encoding="utf-8", newline="") as handle:
            handle.write(existing_page(3))
        before = self.read_bytes(self.page)
        self.assertEqual(3, render_digest.count_story_cards(self.page))

        rc, output = self.run_render([])

        self.assertNotEqual(0, rc,
                            "a 0-story build over a published 3-story page must "
                            "not report success")
        self.assertEqual(before, self.read_bytes(self.page),
                         "the published page changed; an empty run overwrote it")
        self.assertEqual(3, render_digest.count_story_cards(self.page))
        self.assertIn("REFUSED", output)

    def test_an_empty_run_still_writes_where_no_page_exists(self):
        """First build of a slot: nothing to protect, so the recap page ships."""
        rc, output = self.run_render([])
        self.assertEqual(0, rc, output)
        self.assertEqual(0, render_digest.count_story_cards(self.page))
        self.assertTrue(os.path.getsize(self.page) > 0)

    def test_an_empty_run_may_replace_an_already_empty_page(self):
        """A retry over an empty page must not be blocked, or the ladder stalls."""
        with io.open(self.page, "w", encoding="utf-8", newline="") as handle:
            handle.write(existing_page(0))
        self.assertEqual(0, render_digest.count_story_cards(self.page))
        rc, output = self.run_render([])
        self.assertEqual(0, rc, output)
        self.assertEqual(0, render_digest.count_story_cards(self.page))


if __name__ == "__main__":
    unittest.main(verbosity=2)
