#!/usr/bin/env python3
"""tests/render/test_quiet_sections_hidden.py

THE OWNER'S RULE, AS AN EXECUTABLE SPECIFICATION.

    "if there is no news then do not display Gaming - Nothing new from this
    source. just don't display it."

THE PREVIOUS BEHAVIOUR, AND WHY IT WAS WRONG
--------------------------------------------
Every one of the eleven sections rendered on every page, and an empty one
rendered its header plus exactly one status line. On the real 2026-09-29 digest
that put 8 dead sections on a 3-story phone page, 5 of them claiming "No sources
are configured for this section" -- a false statement, since those sources WERE
configured and being fetched. A quiet section is noise a reader cannot act on.

THE LINE THIS DRAWS, AND IT IS THE WHOLE POINT
-----------------------------------------------
A section is omitted ONLY when it has no stories AND its status is quiet. An
ALARM is never hidden. The alarm states (B3 "may be incomplete", B5 "check
<url>") are the only signal that a source the owner TRACKS has gone dark; that
is actionable, and turning a repairable outage into silence is a far worse
defect than a loud box. The owner's request was about a quiet message, not
about alarms.

THIS FILE IS BEHAVIOUR-ONLY ON PURPOSE. It asserts on rendered HTML and on
`section_status()`, never on the name of the helper that implements the rule.
That is deliberate: a test written against `section_is_hidden(...)` would error
with AttributeError against the pre-fix code instead of failing with a
meaningful assertion, and a red that reads "no such attribute" does not
demonstrate that the DEFECT was caught -- only that the API changed. Every test
here therefore fails cleanly, with a diff, on the pre-fix renderer.

Stdlib unittest only; renders to memory or a temp dir, never to docs/.
"""

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import cat_entry, health, TU_HOSTS  # noqa: E402

import build_email  # noqa: E402
import render_digest as rd  # noqa: E402


def cat_entry_empty_hosts(total, ok, empty, failed, skipped, empty_hosts,
                          urls=None):
    """A v2 `by_category` entry.

    Defined HERE rather than imported, and that is on purpose:
    `test_render_honesty.py` has an identical helper, but importing a whole test
    module to reach one function couples this file to that file's fixtures. The
    shape is six keys and is pinned by tests/render/fixtures.py's own `cat_entry`
    for everything except `empty_hosts`.
    """
    return {"total": total, "ok": ok, "empty": empty, "failed": failed,
            "skipped": skipped, "items_found": 0,
            "empty_hosts": list(empty_hosts), "urls": list(urls or [])}


def story(category, title):
    return {
        "title": title, "summary": "A summary line.", "for_you": "",
        "source_name": "Example", "source_url": "https://example.com/%s" % title,
        "reliability": "tier1", "category": category, "priority": 5,
        "published_at": "2026-10-04", "supporting_sources": [],
    }


def render_keys(grouped, health_obj):
    """The section keys that actually rendered, in SECTIONS order."""
    html = rd.render_sections(grouped, health_obj)
    return [key for key, header, _c in rd.SECTIONS
            if rd.esc(header) in html]


def all_status_lines(html):
    return re.findall(r'<p class="section-status[^"]*">(.*?)</p>', html)


#: The three QUIET statuses -- the ones a phone reader cannot act on.
QUIET_LITERALS = (
    rd.UNTRACKED_STATUS,
    "Nothing new from this source today.",
    "Nothing new from this source today (checked",
)


class TestQuietEmptySectionsAreNotRendered(unittest.TestCase):
    def test_an_untracked_section_is_not_displayed_at_all(self):
        """B3a: nothing was ever configured, so there is nothing to say."""
        # The story is in `nepal`, NOT in the section under test: a section that
        # holds a card is never hidden, whatever its status says.
        grouped = {"nepal": [story("nepal", "A real nepal story")]}
        h = health("ok", by_category={})
        # Sanity: the status the old renderer would have shown.
        self.assertEqual(rd.section_status("gaming", 0, h)[0], rd.UNTRACKED_STATUS)
        # The point: that section does not appear in the page.
        html = rd.render_sections(grouped, h)
        self.assertNotIn("GAMING", html)
        for literal in QUIET_LITERALS:
            self.assertNotIn(literal, html)
        self.assertIn("IMPORTANT NEPAL", render_digest_page(grouped, h))

    def test_a_tracked_but_silent_section_is_not_displayed(self):
        """B4: the sources were checked and there was nothing new."""
        grouped = {"nepal": [story("nepal", "A real nepal story")]}
        h = health("ok", by_category={"gaming": cat_entry(6, 6, 0, 0, urls=[])})
        self.assertEqual(rd.section_status("gaming", 0, h)[0],
                         "Nothing new from this source today.")
        html = rd.render_sections(grouped, h)
        self.assertNotIn("GAMING", html)
        self.assertEqual(html.count('<section class="digest-section">'), 1)

    def test_a_source_that_returned_nothing_usable_is_not_displayed(self):
        """B6: reachable, but nothing usable came back. Quiet, so hidden."""
        grouped = {"nepal": [story("nepal", "A real nepal story")]}
        h = health("empty", by_category={
            "gaming": cat_entry_empty_hosts(3, 3, 1, 0, 0, ["khabarhub.com"])})
        self.assertEqual(rd.section_status("gaming", 0, h)[1], "quiet")
        html = rd.render_sections(grouped, h)
        self.assertNotIn("GAMING", html)
        self.assertNotIn("Nothing new from this source today (checked", html)

    def test_all_three_quiet_states_are_hidden_on_all_three_surfaces(self):
        """The website, the email HTML and the plain text must agree."""
        grouped = {"nepal": [story("nepal", "A real nepal story")]}
        h = health("ok", by_category={"gaming": cat_entry(6, 6, 0, 0, urls=[])})
        self.assertEqual(render_keys(grouped, h), ["nepal"])
        email = build_email.render_sections_html(grouped, h)
        text = build_email.render_sections_text(grouped, h)
        for out, label in ((email, "email html"), (text, "email text")):
            self.assertNotIn("GAMING", out, label)
            for literal in QUIET_LITERALS:
                self.assertNotIn(literal, out, "%s: %r" % (label, literal))

    def test_a_page_of_only_quiet_sections_renders_no_section_at_all(self):
        """The extreme case, stated so it is a decision and not a surprise.

        Nothing to show and nothing to alarm about is genuinely an empty page of
        content -- and the run-level `health_banner` still reports the state
        above it, once.
        """
        grouped = {}
        h = health("ok", by_category={"gaming": cat_entry(6, 6, 0, 0, urls=[])})
        self.assertEqual(rd.render_sections(grouped, h), "")
        page = rd.render_page([], "morning", "2026-10-04", "2026-10-04 06:00",
                              {"source_health": h})
        self.assertNotIn('<section class="digest-section">', page)
        self.assertIn('<div class="empty-state">', page,
                      "a page with nothing to say still needs to say so once")


class TestAlarmsAreNeverHidden(unittest.TestCase):
    """The half that must not move."""

    def test_a_tracked_and_unreachable_section_still_renders_its_alarm(self):
        """B5 -- the single most valuable string in the file."""
        grouped = {}
        h = health("degraded", hosts_down=TU_HOSTS,
                   by_category={"education_tu": cat_entry(11, 9, 2, 0)})
        html = rd.render_sections(grouped, h)
        self.assertIn("Source unreachable", html)
        self.assertIn("TU / BSC CSIT", html)
        self.assertEqual(html.count('<section class="digest-section">'), 1)
        lines = all_status_lines(html)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("section-status-alarm", html)

    def test_a_section_with_cards_but_a_failed_source_still_alarms(self):
        """B2 -- partial outage on a section that still has stories."""
        grouped = {"gaming": [story("gaming", "A real gaming story")]}
        h = health("partial", by_category={"gaming": cat_entry(6, 4, 2, 0, urls=[])})
        html = rd.render_sections(grouped, h)
        self.assertIn("2 of 6 sources for this section were unreachable.", html)
        self.assertIn("section-status-alarm", html)
        self.assertEqual(html.count('<article class="card">'), 1)

    def test_an_unmeasurable_tracked_source_still_raises_b3(self):
        """B3 -- health says the category was skipped, so we cannot claim quiet."""
        grouped = {}
        h = health("partial", by_category={"gaming": cat_entry(6, 5, 0, 1, urls=[])})
        html = rd.render_sections(grouped, h)
        self.assertIn("may be incomplete", html)
        self.assertIn("section-status-alarm", html)

    def test_an_untrustworthy_health_object_renders_every_section(self):
        """The strongest form: when we cannot tell untracked from unmeasured,
        every empty section is an alarm, so every one of them must render. If
        this ever fails, an outage has gone completely silent."""
        grouped = {}
        for bad in (None, {}, {"state": "unknown"},
                    {"state": "ok", "by_category": "not-a-dict"}):
            html = rd.render_sections(grouped, bad)
            self.assertEqual(
                html.count('<section class="digest-section">'),
                len(rd.SECTIONS),
                "health=%r: every empty section is an ALARM here, and an alarm "
                "is never hidden" % (bad,))

    def test_alarms_survive_on_the_email_surfaces_too(self):
        grouped = {}
        h = health("degraded", hosts_down=TU_HOSTS,
                   by_category={"education_tu": cat_entry(11, 9, 2, 0)})
        for out, label in ((build_email.render_sections_html(grouped, h), "html"),
                           (build_email.render_sections_text(grouped, h), "text")):
            self.assertIn("Source unreachable", out, label)
            self.assertIn("TU / BSC CSIT", out, label)

    def test_a_zero_story_outage_page_keeps_its_alarms(self):
        """render_page must not replace the sections with the empty-state line
        when there are no stories but there ARE alarms. That would throw away
        the only actionable thing on the page."""
        h = health("degraded", hosts_down=TU_HOSTS,
                   by_category={"education_tu": cat_entry(11, 9, 2, 0)})
        page = rd.render_page([], "morning", "2026-10-04", "2026-10-04 06:00",
                              {"source_health": h})
        self.assertIn("Source unreachable", page)
        # The ELEMENT, not the class name: `.empty-state` is a stylesheet rule
        # that exists whether or not the div is emitted, so matching the bare
        # name would pass even when the empty-state really did replace the
        # sections. This is the same distinction the mobile-design suite makes.
        self.assertNotIn('<div class="empty-state">', page)


class TestTheRuleIsOneRuleInBothModules(unittest.TestCase):
    def test_the_two_renderers_hide_the_same_sections(self):
        """Separate implementations of one contract; they must not drift."""
        cases = (
            ("untracked", {"state": "ok", "by_category": {}}, False),
            ("B4 quiet", health("ok", by_category={
                "gaming": cat_entry(6, 6, 0, 0, urls=[])}), False),
            ("B6 quiet", health("empty", by_category={
                "gaming": cat_entry_empty_hosts(
                    3, 3, 1, 0, 0, ["khabarhub.com"])}), False),
            ("B5 alarm", health("degraded", hosts_down=TU_HOSTS, by_category={
                "gaming": cat_entry(5, 4, 1, 0)}), True),
            ("B3 alarm", health("partial", by_category={
                "gaming": cat_entry(6, 5, 0, 1, urls=[])}), True),
            ("unknown", {"state": "unknown", "by_category": {}}, True),
        )
        for label, h, should_render in cases:
            grouped = {}
            page_keys = render_keys(grouped, h)
            email = build_email.render_sections_html(grouped, h)
            email_keys = [key for key, header, _c in build_email.SECTIONS
                          if header in email]
            self.assertEqual(
                bool(page_keys), should_render,
                "%s: website rendered=%r" % (label, page_keys))
            self.assertEqual(
                bool(email_keys), should_render,
                "%s: email rendered=%r (the two must agree)"
                % (label, email_keys))


def render_digest_page(grouped, health_obj):
    """One full page from a grouped dict, for the `assertIn` sanity check."""
    stories = []
    for key, items in grouped.items():
        for s in items:
            stories.append(dict(s, category=key))
    return rd.render_page(stories, "morning", "2026-10-04", "2026-10-04 06:00",
                          {"source_health": health_obj})


if __name__ == "__main__":
    unittest.main()