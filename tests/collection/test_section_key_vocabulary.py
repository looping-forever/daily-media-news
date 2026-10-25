#!/usr/bin/env python3
"""tests/collection/test_section_key_vocabulary.py

THE CATEGORY/SECTION-KEY VOCABULARY MUST BE ONE VOCABULARY.

THE DEFECT THIS PINS, MEASURED
------------------------------
`source_health["by_category"]` is keyed by the collector plan's category, and
`section_status()` in render_digest.py / build_email.py looks that map up by the
DIGEST SECTION KEY. Two vocabularies, and for five of the eleven sections they
never met:

    world_major  <- collector emitted "world"
    gaming       <- collector emitted "entertainment"
    anime        <- collector emitted "entertainment"
    manga_manhwa_manhua <- collector emitted "entertainment"
    action       <- no plan existed at all

So `by_category` had no entry for those keys, `_status_is_untracked()` returned
True, and the page said "No sources are configured for this section." for five
sections -- a FALSE statement, because sources for four of them were configured
and being fetched every single run.

WHY THIS TEST IS WRITTEN AGAINST `render_digest.SECTIONS` AND NOT AGAINST A
LOCAL LIST. A test that carried its own copy of the section keys could not
detect the two drifting apart, which is the defect. So the collector's output is
pinned AGAINST the renderer's list: the renderer's SECTIONS is the single source
of truth for what a section key IS, and this test fails if the collector ever
emits anything that is not one of them.

`action` IS DELIBERATELY ABSENT FROM `by_category` AND THAT IS CORRECT. It is a
derived section (weather / breaking), it has no source, and fabricating one to
quiet a status line would be the exact class of lie this payload exists to
prevent. The test asserts it is the ONLY missing key, so a future "fix" that
invents an `action` source has to be a deliberate, visible act.

NO NETWORK. `build_all_plans` only builds the plan list; it fetches nothing.
"""

import collections
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import collect_news as cn  # noqa: E402
import render_digest as rd  # noqa: E402

INTERESTS = os.path.join(REPO_ROOT, "config", "interests.yml")

#: The five keys the mismatch broke, and the collector category each one was
#: wrongly filed under. If any of these values reappears as a plan category, the
#: vocabulary has drifted back.
SECTION_KEYS = tuple(key for key, _h, _c in rd.SECTIONS)

#: `action` is the one key with no source BY DESIGN (derived: weather/breaking).
DERIVED_ONLY = ("action",)

#: Direct RSS feeds configured in this change, by section key. Asserted
#: individually so a single mis-tagged feed cannot hide behind eighteen others.
EXPECTED_FEED_SECTIONS = {
    "gaming": 6,
    "anime": 2,
    "manga_manhwa_manhua": 1,
    "world_major": 1,
    "nepal": 3,
    "tech_career": 5,
}


def _plans():
    _excluded, sections = cn.load_interests(INTERESTS)
    return cn.build_all_plans(sections)


class TestCollectorEmitsSectionKeys(unittest.TestCase):
    """The whole point: no category that the digest cannot render."""

    def test_every_plan_category_is_a_digest_section_key(self):
        cats = {p["category"] for p in _plans() if p["category"]}
        unknown = sorted(c for c in cats if c not in SECTION_KEYS)
        self.assertEqual(
            unknown, [],
            "the collector emits categories the digest has no section for, so "
            "their source health is looked up under a key that never exists and "
            "the page falsely reports 'No sources are configured': %r "
            "(valid keys: %r)" % (unknown, list(SECTION_KEYS)))

    def test_the_five_previously_mismatched_keys_all_report_real_health(self):
        """The regression itself, named.

        Each of these four used to be unreachable from `by_category` and so
        rendered a false "no sources configured". Each must now carry at least
        one plan, which is what puts an entry in `by_category`.
        """
        cats = collections.Counter(p["category"] for p in _plans())
        for key in ("world_major", "gaming", "anime", "manga_manhwa_manhua"):
            self.assertGreater(
                cats.get(key, 0), 0,
                "section %r has no plan, so source_health['by_category'] has no "
                "entry for it and section_status() falls through to the false "
                "'No sources are configured for this section.'" % key)
        # ...and `action` is the ONLY key still without one, which is correct.
        missing = [k for k in SECTION_KEYS if cats.get(k, 0) == 0]
        self.assertEqual(missing, list(DERIVED_ONLY),
                         "the only section key with no source should be the "
                         "derived one (%r); anything else missing means a "
                         "configured source lost its plan" % (DERIVED_ONLY,))

    def test_the_old_vocabulary_is_gone_from_the_collector(self):
        """The two words that caused the defect must not reappear.

        Asserted directly rather than only through the consequences, because
        "entertainment" is a perfectly reasonable word and a future edit could
        reintroduce it without anyone intending to break the lookup.
        """
        cats = {p["category"] for p in _plans() if p["category"]}
        self.assertNotIn("entertainment", cats)
        self.assertNotIn("world", cats)

    def test_every_direct_feed_is_tagged_with_a_section_key(self):
        feeds = [p for p in _plans() if p["kind"] == "feed"]
        self.assertEqual(len(feeds), len(cn.DIRECT_FEEDS))
        counts = collections.Counter(p["category"] for p in feeds)
        self.assertEqual(dict(counts), EXPECTED_FEED_SECTIONS)

    def test_every_configured_feed_url_is_unique_and_https(self):
        """Duplicate URLs would silently collapse to one plan per name, and a
        plain-http feed is a claim the collector cannot make."""
        urls = [url for _n, url, _c, _l in cn.DIRECT_FEEDS]
        self.assertEqual(len(set(urls)), len(urls), "duplicate feed URL")
        names = [name for name, _u, _c, _l in cn.DIRECT_FEEDS]
        self.assertEqual(len(set(names)), len(names), "duplicate feed name")
        for url in urls:
            self.assertTrue(url.startswith("https://"), url)

    def test_the_feed_kind_is_declared_where_the_plan_contract_is(self):
        """`build_all_plans`' docstring promises kinds; `feed` must be one."""
        doc = cn.build_all_plans.__doc__
        self.assertIn("feed", doc)


class TestInterestSweepIsCappedNotTruncated(unittest.TestCase):
    """The configured interests are actually searched, within a stated budget."""

    def test_no_hardcoded_slice_replaces_the_declared_cap(self):
        """`games[:6]`-style slices are what this change removed.

        Asserted over the source text of the sweep, because the alternative --
        counting plans for several cap values -- cannot tell a cap from a slice:
        both produce a plan list. The declaration is the single source of truth
        and the sweep must read it.
        """
        import inspect
        src = inspect.getsource(cn.build_all_plans)
        self.assertIn("DEFAULT_INTEREST_TITLE_CAP", src,
                      "build_all_plans must take its cap from the one declared "
                      "constant, not from a literal slice")
        self.assertNotIn("[:6]", src, "a hardcoded [:6] slice is back")
        self.assertNotIn("[:3]", src, "a hardcoded [:3] slice is back")

    def test_the_cap_is_a_cli_override_and_scales_the_plan_count(self):
        base = len(_plans())
        zero = len(cn.build_all_plans(
            cn.load_interests(INTERESTS)[1], interest_cap=0))
        bigger = len(cn.build_all_plans(
            cn.load_interests(INTERESTS)[1],
            interest_cap=cn.DEFAULT_INTEREST_TITLE_CAP * 2))
        self.assertLess(zero, base, "cap=0 must drop the interest queries")
        self.assertGreater(bigger, base,
                           "raising the cap must add queries, or it is not a cap")

    def test_the_cap_is_declared_exactly_once(self):
        import inspect
        src = inspect.getsource(cn)
        # One assignment. The CLI help and the docstring quote the name; neither
        # re-declares a number.
        self.assertEqual(src.count("DEFAULT_INTEREST_TITLE_CAP ="), 1)

    def test_the_sweep_queries_far_more_titles_than_the_old_slices_did(self):
        """The point of the change: 24 -> 48 distinct titles, not 24 -> 4."""
        _excluded, sections = cn.load_interests(INTERESTS)
        plans = cn.build_all_plans(sections)
        titles = set()
        for p in plans:
            if p["kind"] == "gnews" and " OR " in p["name"]:
                q = p["name"].split(":", 2)[2]
                titles.update(t.strip('"') for t in q.split(" OR "))
        self.assertGreaterEqual(
            len(titles), 40,
            "the sweep reached only %d distinct titles; the hardcoded slices "
            "reached 24 of 354 configured" % len(titles))


if __name__ == "__main__":
    unittest.main()