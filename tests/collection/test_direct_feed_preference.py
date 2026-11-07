#!/usr/bin/env python3
"""Offline tests: a direct feed item beats a Google News duplicate.

Google News article ids are encrypted and resolve to nothing offline, so a
Google link that duplicates a publisher feed produced a headline-only card
while the real snippet and publisher URL sat one candidate away. The collector
now keeps the direct item and drops the Google one.

NO NETWORK: everything here is in-memory candidate dicts and pure functions.
"""

import datetime
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import collect_news as cn  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)


def _cand(title, url, hours_ago=None, snippet="", language="en",
          image=None, video=None, source="example.com"):
    """A raw candidate shaped exactly as the collector builds one."""
    dt = None if hours_ago is None else NOW - datetime.timedelta(hours=hours_ago)
    cand = {
        "title": title,
        "url": url,
        "source": source,
        "published": dt.isoformat() if dt else None,
        "snippet": snippet,
        "domain_category": "nepal",
        "language": language,
        "_dt": dt,
        "_kind": "gnews",
    }
    if image is not None:
        cand["image"] = image
    if video is not None:
        cand["video"] = video
    return cand


GOOGLE_URL = "https://news.google.com/rss/articles/CBMiAUF"
HEADLINE = "Nepal cabinet approves annual budget framework"


class TestDirectFeedPreference(unittest.TestCase):
    def test_exact_duplicate_keeps_the_direct_item_and_its_snippet(self):
        """The whole point: the snipped publisher item survives, not the redirect."""
        google = _cand(HEADLINE, GOOGLE_URL)
        direct = _cand(HEADLINE, "https://kathmandupost.com/national/budget",
                       hours_ago=3,
                       snippet="The cabinet approved the annual budget framework "
                               "on Friday, ending three weeks of deadlock.")
        final, _per_domain, stats = cn.postprocess([google, direct], [], 4)
        self.assertEqual([c["url"] for c in final], [direct["url"]])
        self.assertIn("ending three weeks of deadlock", final[0]["snippet"])
        self.assertEqual(stats["google_replaced"], 1)
        self.assertEqual(stats["google_kept"], 0)

    def test_an_outlet_suffix_still_matches(self):
        """Google appends ' - Outlet'; that is the same headline, not a new one."""
        google = _cand(HEADLINE + " - Kathmandu Post", GOOGLE_URL)
        direct = _cand(HEADLINE, "https://kathmandupost.com/national/budget")
        kept, stats = cn.prefer_direct_feeds([google, direct])
        self.assertEqual([c["url"] for c in kept], [direct["url"]])
        self.assertEqual(stats["google_replaced"], 1)

    def test_a_short_overlap_below_the_threshold_does_not_match(self):
        """Containment under 25 characters is a coincidence, not a duplicate."""
        google = _cand("Budget approved by cabinet after long debate", GOOGLE_URL)
        direct = _cand("Budget approved", "https://ekantipur.com/budget")
        kept, stats = cn.prefer_direct_feeds([google, direct])
        self.assertEqual(len(kept), 2, "a fabricated match removed a real item")
        self.assertEqual(stats["google_replaced"], 0)
        self.assertEqual(stats["google_kept"], 1)

    def test_two_direct_items_are_never_merged(self):
        """Direct duplicates are the title dedupe's business, not this pass's."""
        first = _cand(HEADLINE, "https://kathmandupost.com/national/budget")
        second = _cand(HEADLINE, "https://ekantipur.com/news/2026/budget")
        kept, stats = cn.prefer_direct_feeds([first, second])
        self.assertEqual([c["url"] for c in kept],
                         [first["url"], second["url"]])
        self.assertEqual(stats["google_replaced"], 0)

    def test_a_google_item_with_no_duplicate_survives_unchanged(self):
        google = _cand(HEADLINE, GOOGLE_URL, snippet="Cabinet clears budget.")
        direct = _cand("Karnali highway reopens after landslide clearance",
                       "https://kathmandupost.com/karnali/highway")
        kept, stats = cn.prefer_direct_feeds([google, direct])
        self.assertEqual(len(kept), 2)
        self.assertIs(kept[0], google, "the unduplicated Google item was altered")
        self.assertEqual(kept[0]["title"], HEADLINE)
        self.assertEqual(kept[0]["snippet"], "Cabinet clears budget.")
        self.assertEqual(stats["google_kept"], 1)
        self.assertEqual(stats["google_replaced"], 0)

    def test_publication_time_and_image_survive_a_merge(self):
        """Preferring the direct item must not drop facts only Google carried."""
        image = {"url": "https://kathmandupost.com/img/budget.jpg",
                 "source": "media:content"}
        google = _cand(HEADLINE, GOOGLE_URL, hours_ago=2, image=image)
        direct = _cand(HEADLINE, "https://kathmandupost.com/national/budget",
                       snippet="Cabinet approved the framework on Friday.")
        _kept, stats = cn.prefer_direct_feeds([google, direct])
        self.assertEqual(stats["google_replaced"], 1)
        self.assertEqual(direct["published"], google["published"])
        self.assertIsNotNone(direct["_dt"])
        self.assertEqual(direct["image"], image)
        self.assertIn("Friday", direct["snippet"],
                      "the direct snippet must not be overwritten")

    def test_languages_are_never_merged(self):
        google = _cand(HEADLINE, GOOGLE_URL, language="ne")
        direct = _cand(HEADLINE, "https://kathmandupost.com/national/budget",
                       language="en")
        kept, stats = cn.prefer_direct_feeds([google, direct])
        self.assertEqual(len(kept), 2)
        self.assertEqual(stats["google_replaced"], 0)

    def test_the_log_line_counts_both_groups(self):
        google_dup = _cand(HEADLINE, GOOGLE_URL)
        google_only = _cand("Karnali highway reopens after landslide clearance",
                            GOOGLE_URL + "2")
        direct = _cand(HEADLINE, "https://kathmandupost.com/national/budget")
        _kept, stats = cn.prefer_direct_feeds([google_dup, google_only, direct])
        self.assertEqual(
            cn.google_preference_line(stats),
            "google-news: 1 replaced by a direct feed item, 1 kept")


if __name__ == "__main__":
    unittest.main()
