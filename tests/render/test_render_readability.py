#!/usr/bin/env python3
"""tests/render/test_render_readability.py — the owner reads this on a phone.

The owner asked for: "make it easy to understand with bullet points and so on.
make ui even better than it is." These tests pin the fixes that delivered it,
and they fail if a regression puts one of the old defects back:

  * naive pluralisation ("1 stories", "breakings", "gamings") is DELETED, not
    patched - one `plural()` helper, asserted at every call site;
  * .card-grid actually grids, so the existing 2-column CSS rule can fire;
  * a news.google.com/rss/articles/ link is unwrapped and labelled;
  * a deadline gets a loud chip, not only a small meta line;
  * parse_digest_categories (which drives the ntfy teaser) still agrees with
    the page it counts;
  * one clock: the footer is NPT and agrees with the hero, not 5h45m behind it.

Every render goes to a TEMP directory. Nothing here writes to docs/.
"""

import datetime
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import (  # noqa: E402
    TU_HOSTS, cat_entry, health, validated_payload)

import build_email  # noqa: E402
import render_digest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO_ROOT, "templates", "email.html")

GOOGLE_REDIRECT = "https://news.google.com/rss/articles/CBMiSmhww"


def story(category, title, **extra):
    out = {
        "title": title,
        "summary": "A summary line.",
        "for_you": "A reason to care.",
        "source_name": "Kathmandu Post",
        "source_url": "https://example.com/%s" % title.lower().replace(" ", "-"),
        "reliability": "tier1",
        "category": category,
        "priority": 5,
        "published_at": "2026-09-27",
        "supporting_sources": [],
    }
    out.update(extra)
    return out


GOLDEN_STORIES = [
    story("nepal", "Nepal story one"),
    story("nepal", "Nepal story two"),
    story("gaming", "Gaming story one"),
    story("tech_career", "Tech story one",
          deadline="2026-10-01",
          source_url=GOOGLE_REDIRECT,
          supporting_sources=[{"name": "The Kathmandu Post",
                               "url": "https://kathmandupost.com/real-article"}]),
]


class GoldenRenderMixin(object):
    """Renders a full page + email into a TEMP dir exactly once per class."""

    payload = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="render-readability-")
        cls.payload = validated_payload(
            "degraded", stories=GOLDEN_STORIES, notes="TU board unreachable.",
            hosts_down=TU_HOSTS,
            by_category={"education_tu": cat_entry(11, 9, 2, 0)})
        cls.out_dir = os.path.join(cls.tmp, "digests")
        cls.index = os.path.join(cls.tmp, "index.html")
        cls.json_path = os.path.join(cls.tmp, "stories.validated.json")
        with io.open(cls.json_path, "w", encoding="utf-8") as fh:
            import json
            json.dump(cls.payload, fh, ensure_ascii=False)
        cls.rc = render_digest.main([
            "--digest-time", "morning", "--date", "2026-09-27",
            "--email-txt", os.path.join(cls.tmp, "absent-email.txt"),
            "--stories-json", cls.json_path,
            "--out-dir", cls.out_dir, "--index", cls.index])
        cls.page_path = os.path.join(cls.out_dir, "2026-09-27-morning.html")
        with io.open(cls.page_path, encoding="utf-8") as fh:
            cls.page = fh.read()
        with io.open(cls.index, encoding="utf-8") as fh:
            cls.index_html = fh.read()
        cls.email_html = os.path.join(cls.tmp, "email.html")
        cls.email_txt = os.path.join(cls.tmp, "email.txt")
        build_email.main(["--in", cls.json_path, "--out-html", cls.email_html,
                          "--out-text", cls.email_txt, "--template", TEMPLATE])
        with io.open(cls.email_html, encoding="utf-8") as fh:
            cls.email = fh.read()
        with io.open(cls.email_txt, encoding="utf-8") as fh:
            cls.email_text = fh.read()

        # A SECOND digest with exactly one story, so the archive index has to
        # render the singular "1 story" somewhere.
        single = dict(cls.payload, stories=[story("nepal", "Only story")])
        cls.single_json = os.path.join(cls.tmp, "single.json")
        with io.open(cls.single_json, "w", encoding="utf-8") as fh:
            import json
            json.dump(single, fh, ensure_ascii=False)
        render_digest.main([
            "--digest-time", "evening", "--date", "2026-09-27",
            "--email-txt", os.path.join(cls.tmp, "absent-email.txt"),
            "--stories-json", cls.single_json,
            "--out-dir", cls.out_dir, "--index", cls.index])
        with io.open(cls.index, encoding="utf-8") as fh:
            cls.index_html = fh.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Pluralisation
# ---------------------------------------------------------------------------

class TestPluralisation(GoldenRenderMixin, unittest.TestCase):
    def test_plural_helper(self):
        self.assertEqual(render_digest.plural(1, "story", "stories"), "story")
        self.assertEqual(render_digest.plural(0, "story", "stories"), "stories")
        self.assertEqual(render_digest.plural(2, "story", "stories"), "stories")
        self.assertEqual(render_digest.plural(1, "item"), "item")
        self.assertEqual(render_digest.plural(3, "item"), "items")
        self.assertEqual(render_digest.plural(0, "item"), "items")

    def test_build_summary_does_not_naive_append_s(self):
        """The old bug: '%d %ss' on already-plural labels."""
        summary = render_digest.build_summary(
            {"action": 1, "gaming": 2, "world": 1, "manga": 3, "local": 1})
        parts = [p.strip() for p in summary.split(",")]
        self.assertEqual(parts[0], "3 manga")
        self.assertEqual(parts[1], "2 gaming")
        # Ties keep dict order, so compare the four count-1 labels as a set.
        self.assertEqual(set(parts[2:]), {"1 action", "1 world", "1 local"})
        for bogus in ("breakings", "gamings", "worlds", "mangas", "locals"):
            self.assertNotIn(bogus, summary)

    def test_build_summary_empty(self):
        self.assertEqual(render_digest.build_summary({}), "no stories")
        self.assertEqual(render_digest.build_summary(None), "no stories")

    def test_no_bogus_plural_anywhere_in_the_golden_output(self):
        """Every rendered surface, not just build_summary."""
        for name, blob in (("page", self.page), ("index", self.index_html),
                           ("email html", self.email),
                           ("email txt", self.email_text)):
            for bogus in ("breakings", "gamings", "worlds", "mangas", "locals"):
                self.assertNotIn(bogus, blob, "%s contains %r" % (name, bogus))

    def test_hero_and_archive_use_correct_forms(self):
        self.assertIn("%d stories" % len(GOLDEN_STORIES), self.page)
        # The archive index holds both digests: 4 stories and 1 story.
        self.assertIn(">1 story<", self.index_html)
        self.assertIn(">%d stories<" % len(GOLDEN_STORIES), self.index_html)
        self.assertNotIn("1 stories", self.index_html)
        self.assertNotIn("0 stories", self.page)


# ---------------------------------------------------------------------------
# 2. .card-grid actually grids
# ---------------------------------------------------------------------------

class TestCardGrid(GoldenRenderMixin, unittest.TestCase):
    def test_render_succeeded(self):
        self.assertEqual(self.rc, 0)
        self.assertTrue(os.path.isfile(self.page_path))

    def test_every_section_with_items_is_wrapped_in_card_grid(self):
        with_items = 0
        for key, _header, _color in render_digest.SECTIONS:
            if any(s["category"] == key for s in GOLDEN_STORIES):
                with_items += 1
        self.assertEqual(with_items, 3, "fixture should span three categories")
        self.assertEqual(self.page.count('<div class="card-grid">'), with_items)

    def test_cards_sit_inside_the_grid_not_beside_it(self):
        """A card outside any .card-grid is the regression this fixes.

        Invariant per section block: if it holds cards it also holds a
        .card-grid, and that grid opens before the first card.
        """
        blocks = self.page.split('<section class="digest-section">')[1:]
        # RE-POINTED (owner-approved): was `len(SECTIONS)`, i.e. eleven. Four
        # sections render here -- the three holding a card, plus the one holding
        # none and raising an alarm. The per-block invariant below is unchanged
        # and is the actual subject of this test.
        self.assertEqual(len(blocks), 4)
        seen_cards = 0
        for block in blocks:
            cards = block.count('<article class="card">')
            if not cards:
                self.assertNotIn('<div class="card-grid">', block,
                                 "empty section must not open a card-grid")
                continue
            seen_cards += cards
            grid = block.index('<div class="card-grid">')
            first = block.index('<article class="card">')
            self.assertLess(grid, first,
                            "a card rendered before its .card-grid opened")
            self.assertLess(first, block.index("</section>"))
        self.assertEqual(seen_cards, len(GOLDEN_STORIES))

    def test_the_story_grid_is_single_column_and_mobile_only(self):
        """SUPERSEDES `test_two_column_media_rule_survives`.

        That test existed to protect a 2-COLUMN DESKTOP grid
        (`repeat(auto-fill, minmax(340px, 1fr))` at `min-width: 720px`) on a
        page whose `.container` was 1100px wide. The approved design retires all
        of that: the owner has no desktop use case, the content column is capped
        at 480px, and the brief says explicitly not to build or optimise a
        desktop layout. So the rule that must survive is the opposite one - the
        grid is ONE column and nothing may reintroduce a wide multi-column
        layout.

        The structural invariant the old test also protected is unchanged and
        still asserted: sections are plain flow siblings, never grid children.
        """
        self.assertIn(".card-grid {", self.page)
        grid = re.search(r"\.card-grid \{([^}]*)\}", self.page).group(1)
        self.assertIn("grid-template-columns: 1fr", grid)
        # The desktop grid is gone, and no min-width breakpoint can bring it
        # back. Asserted on the ABSENCE of the pattern, so a re-added
        # @media (min-width: ...) rule fails here rather than shipping.
        self.assertNotIn("minmax(340px", self.page)
        self.assertNotIn("@media (min-width", self.page)
        self.assertNotIn("auto-fill", self.page)
        # The old wrapper made <section> the grid child; the sections must be
        # plain flow siblings again.
        self.assertNotRegex(self.page, r'<div class="card-grid">\s*<section')

    def test_sections_are_not_grid_children(self):
        body = self.page.split('<div class="container">', 1)[1]
        self.assertNotIn('<div class="card-grid">\n    <section', body)


# ---------------------------------------------------------------------------
# 3. Google News redirect label
# ---------------------------------------------------------------------------

class TestGoogleNewsLabel(GoldenRenderMixin, unittest.TestCase):
    def test_resolve_article_url_prefers_supporting_source(self):
        for resolver in (render_digest.resolve_article_url, build_email.resolve_article_url):
            url, via = resolver(GOLDEN_STORIES[3])
            self.assertEqual(url, "https://kathmandupost.com/real-article")
            self.assertTrue(via)
            plain = story("nepal", "Plain")
            self.assertEqual(resolver(plain), (plain["source_url"], False))

    def test_redirect_without_supporting_source_keeps_url_but_labels(self):
        lonely = story("nepal", "Lonely", source_url=GOOGLE_REDIRECT)
        for resolver in (render_digest.resolve_article_url, build_email.resolve_article_url):
            url, via = resolver(lonely)
            self.assertEqual(url, GOOGLE_REDIRECT)
            self.assertTrue(via)

    def test_non_redirect_is_never_labelled(self):
        for resolver in (render_digest.resolve_article_url, build_email.resolve_article_url):
            _, via = resolver(story("nepal", "Direct", source_url="https://kathmandupost.com/x"))
            self.assertFalse(via)

    def test_chip_and_unwrapped_link_in_the_website(self):
        self.assertIn("via Google News", self.page)
        self.assertIn('href="https://kathmandupost.com/real-article"', self.page)

    def test_label_in_both_email_surfaces(self):
        self.assertIn("via Google News", self.email)
        self.assertIn("(via Google News)", self.email_text)
        self.assertIn("https://kathmandupost.com/real-article", self.email_text)

    def test_chip_not_emitted_for_stories_without_a_redirect(self):
        card = render_digest.build_story_card(story("nepal", "Plain"))
        self.assertNotIn("via Google News", card)


# ---------------------------------------------------------------------------
# 4. Deadline emphasis
# ---------------------------------------------------------------------------

class TestDeadlineEmphasis(GoldenRenderMixin, unittest.TestCase):
    def test_deadline_chip_on_the_card_and_in_the_email(self):
        self.assertIn("chip-deadline", self.page)
        self.assertIn("Deadline 2026-10-01", self.page)          # card chip
        self.assertIn("Deadline 2026-10-01", self.email)         # email chip
        self.assertIn("Deadline: 2026-10-01", self.email_text)    # plain text
        # The pre-existing card-deadline line is kept as well.
        self.assertIn("card-deadline", self.page)

    def test_no_deadline_chip_without_a_deadline(self):
        card = render_digest.build_story_card(story("nepal", "No deadline"))
        self.assertNotIn("chip-deadline", card)
        self.assertNotIn("card-deadline", card)

    def test_deadline_is_escaped(self):
        card = render_digest.build_story_card(
            story("nepal", "Evil", deadline='"><script>alert(1)</script>'))
        self.assertNotIn("<script>", card)
        self.assertIn("&lt;script&gt;", card)


# ---------------------------------------------------------------------------
# 5. parse_digest_categories still agrees with the page
# ---------------------------------------------------------------------------

class TestTeaserAgreesWithPage(GoldenRenderMixin, unittest.TestCase):
    def test_counts_match_the_input_stories(self):
        counts = render_digest.parse_digest_categories(self.page_path)
        self.assertEqual(counts, {"nepal": 2, "gaming": 1, "tech": 1})
        self.assertEqual(sum(counts.values()), len(GOLDEN_STORIES))

    def test_status_lines_do_not_inflate_the_count(self):
        """3 sections hold cards; 4 render (those 3 plus the one raising an
        alarm). If a status line ever contained card markup, the teaser would
        silently drift.

        RE-POINTED (owner-approved). This used to assert eleven sections and 8
        status lines, encoding the old rule that a section always renders. The
        count that matters now is CARDS vs SECTIONS: a status line is still not a
        card, and hiding the quiet sections must not have changed how many cards
        the teaser counts. `test_only_the_genuinely_unreachable_section_carries_
        the_alarm` covers which sections render and why."""
        self.assertEqual(self.page.count('<section class="digest-section">'), 4)
        self.assertEqual(self.page.count('<article class="card">'),
                         len(GOLDEN_STORIES))
        # Only the alarm section still emits a status line at all: 4 sections
        # rendered, 3 of which hold cards and are therefore silent (B1).
        self.assertEqual(len(re.findall(r'<p class="section-status[^"]*">', self.page)), 1)

    def test_index_teaser_is_the_summary_of_the_same_counts(self):
        counts = render_digest.parse_digest_categories(self.page_path)
        self.assertIn(render_digest.build_summary(counts), self.index_html)

    def test_counts_survive_a_degraded_render(self):
        """The outage path must not change the counts the teaser reports."""
        counts = render_digest.parse_digest_categories(self.page_path)
        self.assertIn("⚠️ Source unreachable — check https://iost.tu.edu.np/ "
                      "or https://tu.edu.np/", self.page)
        self.assertEqual(sum(counts.values()), len(GOLDEN_STORIES))

    def test_parse_returns_none_for_a_missing_file(self):
        self.assertIsNone(render_digest.parse_digest_categories(
            os.path.join(self.tmp, "nope.html")))


# ---------------------------------------------------------------------------
# 6. One clock
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7. THE READER SURFACE: what the owner actually sees
# ---------------------------------------------------------------------------
# A fixture that reproduces the shape of the real 2026-09-29 morning digest,
# because that is the page the defects were reported on: THREE stories spread
# over three sections, a degraded health payload in which only SOME categories
# are tracked at all, a research note written in the collector's build-log
# vocabulary, and a weather engine that had something to say.
#
# The live page carried 11 warning-flavoured elements on a 3-story page (3 in
# the header + 8 status boxes, 5 of them false), a `Your day` block that existed
# only in the <style> block, `★ P5` / `tier1` / `tier2` / `event:` / a source id
# on every card, and a research note cut mid-word. Every render here goes to a
# TEMP directory. Nothing writes to docs/.

LIVE_DATE = "2026-09-29"

#: Build-log tokens the collector writes and the reader must never see.
INTERNAL_TOKENS = ("P5", "tier1", "tier2", "event:", "notice:tu.edu.np",
                   "TimeoutError", "open-meteo")

#: The real note, verbatim from the live page, internals and all.
LIVE_NOTE = ("Failing sources, stated plainly: notice:tu.edu.np and "
             "notice:iost.tu.edu.np both failed with TimeoutError, so no direct "
             "TU/IOST homepage scrape this run; TU news was covered via Google "
             "News plus a fetch of the IOE Examination Control Division board "
             "which is the notice board the faculty publishes exam schedules on")

LIVE_WEATHER = [
    {"id": "U", "text": "Take an umbrella - 70% chance of rain 15:00-17:59",
     "priority": 4},
    {"id": "J", "text": "Take a jacket - feels like 9C 07:00-11:59", "priority": 6},
]


def live_shaped_story(title, category, **extra):
    """A story carrying the build-log fields the live page leaked."""
    out = {
        "title": title,
        "summary": "A summary line.",
        "for_you": "",
        "label": "NEW",
        # These four are collector internals and every one of them used to be
        # printed on the card.
        "source_name": "open-meteo",
        "reliability": "tier2",
        "event_date": LIVE_DATE,
        "priority": 5,
        "category": category,
        "published_at": LIVE_DATE,
        "source_url": "https://example.com/%s" % title.lower().replace(" ", "-"),
        "supporting_sources": [],
    }
    out.update(extra)
    return out


class TestReaderSurface(GoldenRenderMixin, unittest.TestCase):
    """The five reported defects, asserted on a real render of a real shape."""

    payload = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="render-reader-surface-")
        stories = [
            live_shaped_story("Example City thunderstorm day", "local_city",
                              source_name="open-meteo"),
            # Exactly one story carries a for_you, so "rendered" and "absent
            # with no empty label" are both observable in the same page.
            live_shaped_story("Rain eases towards evening", "local_city",
                              source_name="Ratopati",
                              for_you="leave before 16:00, the road floods"),
            live_shaped_story("KYC window open all week", "bank_local",
                              source_name="Example Bank", reliability="tier1",
                              published_at="2026-09-16"),
        ]
        payload = validated_payload(
            "degraded", stories=stories, notes=LIVE_NOTE, hosts_down=TU_HOSTS,
            by_category={
                # Tracked and healthy, but nothing today -> B4, benign.
                "local_city": cat_entry(4, 4, 0, 0, urls=[]),
                "bank_local": cat_entry(3, 3, 0, 0, urls=[]),
                "world_major": cat_entry(6, 6, 0, 0, urls=[]),
                "college_local": cat_entry(3, 3, 0, 0, urls=[]),
                # Tracked, and a host ANSWERED but returned nothing usable ->
                # B6. Also benign: it is a fact about the day, not a failure.
                "nepal": {"total": 5, "ok": 5, "failed": 0, "skipped": 0,
                          "empty_hosts": ["khabarhub.com"], "urls": []},
                # Tracked and DOWN -> the one real alarm (B5).
                "education_tu": cat_entry(11, 9, 2, 0),
            })
        payload["date"] = LIVE_DATE
        payload["weather_advice"] = LIVE_WEATHER
        payload["notes"] = LIVE_NOTE
        cls.payload = payload

        cls.json_path = os.path.join(cls.tmp, "stories.validated.json")
        with io.open(cls.json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        cls.out_dir = os.path.join(cls.tmp, "digests")
        cls.index = os.path.join(cls.tmp, "index.html")
        cls.rc = render_digest.main([
            "--digest-time", "morning", "--date", LIVE_DATE,
            "--email-txt", os.path.join(cls.tmp, "absent-email.txt"),
            "--stories-json", cls.json_path,
            "--out-dir", cls.out_dir, "--index", cls.index])
        cls.page_path = os.path.join(cls.out_dir, "%s-morning.html" % LIVE_DATE)
        with io.open(cls.page_path, encoding="utf-8") as fh:
            cls.page = fh.read()
        cls.email_html_path = os.path.join(cls.tmp, "email.html")
        cls.email_txt_path = os.path.join(cls.tmp, "email.txt")
        build_email.main(["--in", cls.json_path, "--out-html", cls.email_html_path,
                          "--out-text", cls.email_txt_path, "--template", TEMPLATE])
        with io.open(cls.email_html_path, encoding="utf-8") as fh:
            cls.email = fh.read()
        with io.open(cls.email_txt_path, encoding="utf-8") as fh:
            cls.email_text = fh.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- 1. the `Your day` block is never silently absent -------------------

    def test_your_day_container_is_in_the_body_not_only_in_the_css(self):
        """BUG 1. On the live page the CSS carried `.your-day` and
        `.your-day-label` and the body carried NEITHER, so a shipped feature
        rendered as if it did not exist. Both halves are asserted, because the
        bug was precisely that the styling survived without the markup."""
        self.assertEqual(self.rc, 0)
        self.assertIn('<div class="your-day">', self.page)
        self.assertIn('<p class="your-day-label">', self.page)
        # And it is never a heading with nothing under it. This digest has one
        # for_you line, so that is the one line the block must carry.
        #
        # THE EXTRACTION IS NOW NESTING-AWARE, and that is the whole change
        # here. The old one was `split("</div>", 1)[0]`, which assumed the
        # label and the list were the block's only children. The approved design
        # puts a header ROW between them - label, spacer, weather glyph - so the
        # first `</div>` is now that row's and the naive split would have
        # "passed" against an EMPTY block. The guard this test exists for is
        # BUG 1 (styling present, markup absent), so weakening its extraction
        # would have quietly disarmed it. Matching the block's own closing tag
        # keeps the assertion strictly stronger than before: the `<li>` must be
        # INSIDE the block, not merely after it.
        block = self.page.split('<div class="your-day">', 1)[1].split("</ul>", 1)[0]
        self.assertNotIn("</div></div></div>", block, "block nesting changed")
        self.assertIn("<li>", block)
        self.assertIn("leave before 16:00, the road floods", block)
        # The header row's weather glyph rides in the same block.
        self.assertIn('class="weather-icon"', block)
        # And the same on the email surfaces, so the block cannot vanish there.
        self.assertIn("leave before 16:00, the road floods", self.email)
        self.assertIn("Your day", self.email_text)

    def test_your_day_renders_the_fallback_rather_than_nothing(self):
        """The empty case itself, isolated: `your_day_lines` hands the renderer
        nothing (weather-only, rendered in its own block) and the block still
        renders the documented fallback instead of vanishing.

        This is the exact live-page shape: 3 stories, no deadline, no for_you,
        2 weather lines. The live page produced a page with the CSS and no
        markup; this produces a page with both."""
        empty_html = render_digest.render_your_day([])
        self.assertIn('<div class="your-day">', empty_html)
        self.assertIn(render_digest.NOTHING_TODAY, empty_html)
        self.assertIn("<li>", empty_html)
        # Non-empty input is unchanged: the fallback is not prepended.
        one = render_digest.render_your_day(["a real line"])
        self.assertNotIn(render_digest.NOTHING_TODAY, one)
        # And the two owned email surfaces agree. They take the resolved
        # weather STRINGS (resolve_weather_advice has already run by then),
        # not the engine's raw dicts.
        texts = [w["text"] for w in LIVE_WEATHER]
        self.assertIn(render_digest.NOTHING_TODAY,
                      build_email.your_day_html(texts, [], LIVE_DATE))
        self.assertIn("Your day", build_email.your_day_text(texts, [], LIVE_DATE))

    # -- 2. build-log internals are gone from the reader surfaces -----------

    def test_build_log_internals_are_absent_from_the_reader_html(self):
        for token in INTERNAL_TOKENS:
            self.assertNotIn(token, self.page,
                             "%r is build-log output and must not be on the page"
                             % token)
            self.assertNotIn(token, self.email,
                             "%r leaked into the email html" % token)
        # ...while the PAYLOAD still carries every one of them, because the
        # pipeline and the logs need them. Hiding them on a reader surface is
        # not deleting them.
        raw = io.open(self.json_path, encoding="utf-8").read()
        for token in ("tier1", "tier2", "event_date", "notice:tu.edu.np",
                      "TimeoutError", "open-meteo", '"priority": 5'):
            self.assertIn(token, raw, "the payload lost %r" % token)

    def test_the_verdict_comes_before_your_day(self):
        """The owner's readability change: read WHAT is wrong with the page
        BEFORE your own day, instead of scrolling past it to find out.

        Asserted on ORDER, not on mere presence, so a future edit that quietly
        moves the block back below `Your day` is caught.
        """
        self.assertLess(self.page.index('class="health-banner"'),
                        self.page.index('class="your-day"'),
                        "the health banner must precede `Your day`")

        # The counterpart, and what makes the first half non-vacuous: on a
        # healthy day there is no banner at all, so `Your day` must be the FIRST
        # thing inside the hero -- nothing may be inserted above it.
        healthy = validated_payload("ok")
        page = render_digest.render_page(healthy["stories"], "morning", LIVE_DATE,
                                         "2026-09-27 06:31", healthy)
        self.assertNotIn('class="health-banner"', page,
                         "a healthy page must not carry a banner")
        hero = page.split('<section class="hero">', 1)[1].split("</section>", 1)[0]
        self.assertTrue(hero.lstrip().startswith('<div class="your-day"'),
                        "`Your day` must be first in a healthy hero, got: %r"
                        % hero[:120])

    def test_the_research_note_survives_with_its_hosts_named(self):
        """The note is a REAL warning and is not softened. Only the collector's
        vocabulary is re-worded: the hosts that failed are still named, and a
        timeout is still called a failure."""
        note = re.search(r'<p class="health-note">(.*?)</p>', self.page, re.S)
        self.assertIsNotNone(note, "the research note disappeared")
        body = note.group(1)
        self.assertIn("tu.edu.np", body)
        self.assertIn("iost.tu.edu.np", body)
        self.assertIn("a timeout", body)

    def test_a_long_note_is_cut_on_a_word_boundary_not_mid_word(self):
        """The live note ended "…the IOE Examination Control Divis…". A cut that
        leaves half a word is a cut that is telling the reader nothing."""
        for mod in (render_digest, build_email):
            note = mod.capped_note(LIVE_NOTE, limit=120)
            shown = note[len("⚠️ Research note: "):]
            self.assertTrue(shown.endswith("…"), shown[-20:])
            self.assertNotIn(" ", shown[-1:], "ellipsis must follow a space cut")
            last = shown[:-1].rstrip().split(" ")[-1]
            self.assertIn(last, " ".join(LIVE_NOTE.split()),
                          "cut mid-word at %r" % last)
            self.assertLessEqual(len(shown), 120)
        # A note with no whitespace at all still gets a hard cut: the cap is a
        # cap, and a single 5000-char token must not blow past it.
        for mod in (render_digest, build_email):
            self.assertEqual(len(mod.capped_note("x" * 5000)) - len(
                "⚠️ Research note: "), 240)

    # -- 3. the category is not printed twice per article -------------------

    def test_the_category_chip_no_longer_duplicates_the_section_header(self):
        """Every card sat under the <h2> of its own section, so the chip
        repeated the header verbatim on every article."""
        for blob, name in ((self.page, "website"), (self.email, "email html")):
            self.assertNotIn('class="chip chip-cat"', blob, name)
        # The header itself is still there, exactly once per section.
        self.assertEqual(self.page.count("LOCAL &amp; CITY"), 1)
        self.assertEqual(self.page.count("MY BANK"), 1)
        # The genuinely useful chips are NOT collateral damage.
        self.assertIn("chip-deadline", self.page)
        self.assertIn("[NEW]", self.page)

    # -- 4. quiet for benign, alarm only for a real unreachable source ------

    def test_only_the_genuinely_unreachable_section_carries_the_alarm(self):
        """This fixture is the whole contract in one page: it tracks six
        categories and fills three of the four status shapes at once.

        RE-POINTED (owner-approved). This used to assert 8 QUIET lines and eleven
        sections, which is the behaviour the owner asked to stop: "if there is no
        news then do not display Gaming - Nothing new from this source. just
        don't display it."

        THE ALARM HALF IS THE PART THAT MUST NOT MOVE, and it is asserted first
        and hardest. This page tracks six categories; five of them are up (B4) or
        answered-with-nothing (B6), and exactly one -- education_tu -- is DOWN.
        So:

          education_tu       0 cards, failed=2 -> B5 ALARM  -> RENDERS
          local_city       2 cards                        -> RENDERS
          bank_local    1 card                         -> RENDERS
          world_major        0 cards, healthy  -> B4 quiet  -> hidden
          college_local       0 cards, healthy  -> B4 quiet  -> hidden
          nepal              0 cards, empty host -> B6 quiet -> hidden
          action/gaming/anime/manga_manhwa_manhua/tech_career
                             untracked       -> B3a quiet   -> hidden
        """
        alarms = re.findall(
            r'<p class="section-status section-status-alarm">(.*?)</p>', self.page)
        quiet = re.findall(r'<p class="section-status">(.*?)</p>', self.page)
        # THE ALARM SURVIVES. A tracked source went dark and the page says so.
        self.assertEqual(len(alarms), 1, alarms)
        self.assertIn("Source unreachable", alarms[0])
        self.assertIn("iost.tu.edu.np", alarms[0])
        # THE QUIET LINES ARE GONE -- B4, B6 and B3a alike. All three are facts
        # about a quiet day that the reader can do nothing with, and on a phone
        # they were most of the page.
        self.assertEqual(quiet, [], quiet)
        self.assertNotIn(render_digest.UNTRACKED_STATUS, self.page)
        self.assertNotIn("Nothing new from this source today", self.page)
        # Exactly three sections survive, and they are named rather than counted,
        # so a section wrongly dropped fails here just as loudly as one wrongly
        # shown.
        rendered = [key for key, header, _c in render_digest.SECTIONS
                    if render_digest.esc(header) in self.page]
        self.assertEqual(rendered, ["local_city", "education_tu",
                                    "bank_local"], rendered)
        self.assertEqual(self.page.count('<section class="digest-section">'), 3)

    def test_the_benign_status_is_not_styled_like_the_alarm(self):
        """D2, measured on the CSS rather than asserted as a string. The base
        `.section-status` rule must carry no red bar and no red background, and
        the amber must live only on the modifier.

        TWO LITERALS MOVED, AND ONLY TWO, because two style values legitimately
        changed. Every property assertion is untouched:

          `#aaa` -> `#9BA1AC` (--muted). The quiet tone had to join the approved
            palette so it could be measured against the page background the same
            way every other colour on this page is. It is measurably QUIETER
            than what it replaced (7.33:1 on #0E1014, against 8.20:1 for
            #aaa), so this is not a rename that could weaken the distinction -
            and `test_the_quiet_tone_is_measurably_dimmer_than_the_alarm_tone`
            below now pins that relationship directly rather than trusting a
            literal.
          `0.95rem` -> `11px`. 15.2px was LARGER than the 15px story titles.
            See test_no_metadata_text_outranks_the_story_titles.

        The assertion that both tones stay the SAME size is the one that matters
        most and it is still here: if a future edit gives the alarm its own
        font-size, colour stops being the only thing separating the two claims
        and D2 is broken again.
        """
        base = re.search(r"\.section-status \{([^}]*)\}", self.page).group(1)
        self.assertIn("#9BA1AC", base)
        self.assertNotIn("#fbbf24", base.lower())
        self.assertNotIn("#dc2626", base)
        self.assertIn("border-left: 3px solid transparent", base)
        mod = re.search(r"\.section-status-alarm \{([^}]*)\}", self.page).group(1)
        self.assertIn("#fbbf24", mod.lower())
        self.assertIn("#dc2626", mod)
        # Both tones are the same size, so colour is the only difference.
        self.assertNotIn("font-size", mod,
                         "the alarm must not declare its own font-size")
        size = re.search(r"\.section-status \{[^}]*font-size:([^;]+);", self.page).group(1)
        self.assertEqual(size.strip(), "11px")
        # And the size is now pinned as an approved value, not just "smaller".
        self.assertEqual(render_digest.D_MUTED, "#9BA1AC")

    def test_the_quiet_tone_is_measurably_dimmer_than_the_alarm_tone(self):
        """The D2 property, stated as a RELATIONSHIP so it survives a re-tint.

        Pinning "#aaa is in the base rule and #fbbf24 is not" only proves the
        literal moved. What must hold is that a reader can still tell the two
        claims apart at a glance, which means the quiet tone has to be the
        lower-contrast one against the page background. This compares computed
        WCAG relative luminance rather than the hex strings, so recolouring
        either tone - in either direction - fails here.
        """
        def rule(pattern):
            return re.search(pattern + r" \{([^}]*)\}", self.page).group(1)

        def colour_of(body):
            found = re.search(r"color:\s*(#[0-9a-fA-F]{3,6});", body)
            self.assertIsNotNone(found, "no color: declaration")
            return found.group(1)

        def luminance(hex_color):
            h = hex_color.lstrip("#")
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            channels = []
            for i in (0, 2, 4):
                c = int(h[i:i + 2], 16) / 255.0
                channels.append(c / 12.92 if c <= 0.03928
                                else ((c + 0.055) / 1.055) ** 2.4)
            return (0.2126 * channels[0] + 0.7152 * channels[1]
                    + 0.0722 * channels[2])

        def ratio(fg, bg):
            hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        base = colour_of(rule(r"\.section-status"))
        alarm = colour_of(rule(r"\.section-status-alarm"))
        self.assertNotEqual(base.lower(), alarm.lower(),
                            "the two tones must not be the same colour")
        quiet_ratio = ratio(base, render_digest.D_BG)
        alarm_ratio = ratio(alarm, render_digest.D_BG)
        self.assertLess(quiet_ratio, alarm_ratio,
                        "the quiet tone must be the lower-contrast one "
                        "(quiet %.2f:1, alarm %.2f:1 on %s)"
                        % (quiet_ratio, alarm_ratio, render_digest.D_BG))
        # And the quiet tone is still comfortably readable at its new size: 11px
        # is NORMAL text, so WCAG AA's 4.5:1 is the bar, not the 3:1 that only
        # applies to large text.
        self.assertGreaterEqual(quiet_ratio, 4.5,
                                "the quiet status line is no longer readable "
                                "at 11px (%.2f:1 on %s)"
                                % (quiet_ratio, render_digest.D_BG))

    def test_the_email_surfaces_use_the_same_two_tones(self):
        """The ALARM tone reaches the email. The QUIET tone provably cannot.

        RE-POINTED (owner-approved), and it records a CONSEQUENCE of the hiding
        rule rather than pretending otherwise. This test used to assert the quiet
        style was present, via a payload whose empty sections were all untracked
        and so carried B3a. Those sections are now omitted rather than labelled,
        and that makes `STATUS_QUIET_STYLE` UNREACHABLE in the email:

          render_sections_html emits a status div only `if status:`, and a
          status is only non-empty when the tone is an ALARM -- an empty section
          with a quiet tone is now hidden before that point, and a section with
          cards is silent (B1). So every status line the email can render is an
          alarm, and every one of them uses STATUS_ALARM_STYLE.

        That is asserted here as a fact rather than worked around. The constant
        is deliberately KEPT rather than deleted: it is the documented default
        tone ("a forgotten tone renders quiet rather than loud"), and a future
        change that makes a quiet status visible again would want it. But a test
        that claimed it was reachable would be lying, and this one does not.
        """
        self.assertIn(build_email.STATUS_ALARM_STYLE, self.email)
        self.assertIn("Source unreachable", self.email)
        # Every status line in this payload is the alarm tone, so the quiet style
        # appears nowhere -- and that is the assertion, not an omission.
        self.assertNotIn(build_email.STATUS_QUIET_STYLE, self.email)
        self.assertNotIn(build_email.STATUS_QUIET_STYLE, self.email_text)
        # And the reason it is unreachable is itself pinned, so this cannot rot
        # into a false claim: give the renderer a section that HAS cards and a
        # quiet tone (B1) and confirm it renders silently, with no status box.
        quiet_with_cards = validated_payload(
            "ok",
            by_category={"gaming": {"total": 3, "ok": 3, "failed": 0,
                                    "skipped": 0, "urls": []}},
            stories=[dict(live_shaped_story("A gaming headline", "gaming"))])
        grouped = build_email.group_by_category(quiet_with_cards["stories"])
        self.assertEqual(
            build_email.section_status("gaming", 1,
                                       quiet_with_cards["source_health"]),
            ("", ""), "B1 must stay silent, which is why the quiet style is unused")
        self.assertNotIn(
            build_email.STATUS_QUIET_STYLE,
            build_email.render_sections_html(
                grouped, quiet_with_cards["source_health"]))

        # AND THE C LITERAL IS NOT RENDERED ANYWHERE. This is the re-pointed form
        # of the old `assertIn(UNTRACKED_STATUS, ...)`: the constant still EXISTS
        # and section_status still returns it (see
        # test_both_renderers_agree_on_every_status_literal_and_tone), but a quiet
        # empty section is now omitted, so it must not appear on any surface.
        self.assertNotIn(render_digest.UNTRACKED_STATUS, self.email)
        self.assertNotIn(render_digest.UNTRACKED_STATUS, self.email_text)

    def test_both_renderers_agree_on_every_status_literal_and_tone(self):
        """The two renderers are separate implementations of one contract and
        must not drift, literal AND tone, case by case.

        The C LITERAL IS STILL PINNED HERE, deliberately, and that is not an
        oversight. `UNTRACKED_STATUS` and `section_status()` are unchanged: the
        owner asked for quiet empty sections not to be DISPLAYED, not for the
        renderer to stop being able to describe one. The hiding happens one layer
        up, in the section-block renderers, and the test below proves the two
        modules agree on it as well -- so the constant staying here and the
        literal staying off the page are not in tension.
        """
        for mod in (render_digest, build_email):
            self.assertEqual(mod.UNTRACKED_STATUS,
                             "No sources are configured for this section.")
            self.assertEqual(mod.STATUS_ALARM, "alarm")
            self.assertEqual(mod.STATUS_QUIET, "quiet")
        for state, entry, items in (
                ("ok", {"total": 5, "ok": 5, "failed": 0, "skipped": 0, "urls": []}, 0),
                ("partial", {"total": 5, "ok": 4, "failed": 0, "skipped": 1, "urls": []}, 0),
                ("degraded", {"total": 5, "ok": 4, "failed": 1, "skipped": 0,
                              "urls": ["https://x.example/"]}, 0),
                ("degraded", {"total": 5, "ok": 4, "failed": 1, "skipped": 0,
                              "urls": ["https://x.example/"]}, 2),
                ("ok", {"total": 5, "ok": 5, "failed": 0, "skipped": 0, "urls": []}, 2)):
            h = {"state": state, "by_category": {"gaming": entry}}
            self.assertEqual(render_digest.section_status("gaming", items, h),
                             build_email.section_status("gaming", items, h),
                             (state, entry, items))
        # And the untracked branch, which the matrix above cannot reach because
        # it needs a category ABSENT from a trustworthy map.
        for state in ("ok", "partial", "degraded", "empty"):
            h = {"state": state, "by_category": {"nepal": {
                "total": 3, "ok": 3, "failed": 0, "skipped": 0, "urls": []}}}
            self.assertEqual(render_digest.section_status("anime", 0, h),
                             build_email.section_status("anime", 0, h),
                             state)
            self.assertEqual(render_digest.section_status("anime", 0, h)[1],
                             "quiet", state)

    def test_both_renderers_agree_on_which_sections_are_hidden(self):
        """The hiding rule is a second shared contract, so it is pinned on both
        modules over the FULL matrix rather than through one fixture.

        Without this, one renderer could start hiding an alarm section (a source
        the owner tracks goes dark and the email says nothing) while the other
        kept it, and every literal-level test above would still pass.
        """
        cases = (
            # (count, tone, hidden)
            (0, "quiet", True),    # B3a / B4 / B6 -- the owner's quiet noise
            (0, "alarm", False),   # B3 / B5 -- a tracked source went dark
            (2, "quiet", False),   # has stories; B1/B2
            (2, "alarm", False),   # has stories AND a failed source (B2)
        )
        for count, tone, hidden in cases:
            self.assertEqual(
                render_digest.section_is_hidden(count, tone), hidden,
                ("render_digest", count, tone))
            self.assertEqual(
                build_email.section_is_hidden(count, tone), hidden,
                ("build_email", count, tone))
        # And the end-to-end half on the golden fixture: the website page and the
        # email must agree on WHICH sections survive, not merely on the rule.
        # Counted by section KEY, not by header text: the two modules spell
        # their headers differently on purpose (build_email carries them already
        # HTML-escaped, "WORLD &#8212; MAJOR EVENTS"; render_digest carries the
        # raw form and escapes at render time), so only the KEY is comparable.
        email_headers = dict((k, h) for k, h, _c in build_email.SECTIONS)
        rendered_in_email = [key for key, _h, _c in render_digest.SECTIONS
                             if email_headers[key] in self.email]
        rendered_on_page = len(re.findall(
            r'<h2 class="section-header"', self.page))
        self.assertEqual(len(rendered_in_email), rendered_on_page)
        self.assertEqual(rendered_in_email,
                         ["local_city", "education_tu", "bank_local"])

    # -- 5. a real warning is never weakened --------------------------------

    def test_the_real_unreachable_banner_still_renders(self):
        self.assertIn("⚠️ Some sources were unreachable", self.page)
        self.assertIn("this digest may be missing items", self.page)
        self.assertIn("Down: iost.tu.edu.np, tu.edu.np", self.page)
        self.assertIn("health-banner", self.page)
        # And the honest cutoff contract: the timestamp is kept with its
        # qualifier, because the sources DID answer for other sections.
        self.assertIn("Information checked through", self.page)
        self.assertIn("some sources were unreachable", self.page)

# -- the type scale, as the approved mobile design defines it ------------

    def test_the_type_scale_matches_the_approved_mobile_design(self):
        """SUPERSEDES D1, and the reason is worth stating rather than burying.

        D1 existed because of a specific defect: the section header computed to
        15.2px while the article headlines beneath it computed to 17.3px, so the
        second-smallest text on the page was the thing organising it. D1 fixed
        that by making the section header LARGE (20-25.6px), on the assumption
        that a section header is a heading competing with its articles.

        The design the owner approved on 2026-10-03 removes that assumption. The
        coloured header bar is gone and the section name is now a QUIET LABEL -
        a 6px dot and 11px uppercase text. So the approved scale INVERTS D1's
        ordering on purpose: the section label is the smallest text on the page,
        and nothing below the page title (21px) is larger than the story title
        (16px). That is the design's whole point - the eye should land on
        stories, not on section furniture.

        So this is a re-specification against the approved values, not a
        weakened assertion. It is if anything stricter than D1: D1 allowed a
        clamp() whose value MOVED with the viewport, because the old page had to
        work at 320px and at 1920px. This page is mobile-only, so every size is
        a fixed px value that must be exactly the approved one, and the absence
        of clamp() is itself asserted below.
        """
        def css_size(selector):
            rule = re.search(r"\.%s \{([^}]*)\}" % selector, self.page)
            self.assertIsNotNone(rule, "no CSS rule for .%s" % selector)
            found = re.search(r"font-size:([^;]+);", rule.group(1))
            self.assertIsNotNone(found, ".%s declares no font-size" % selector)
            return found.group(1).strip()

        page_title = css_size("topbar-title")
        story_title = css_size("card-title")
        section_label = css_size("section-header")

        # The approved numbers, exactly.
        self.assertEqual(page_title, "21px")
        # RE-APPROVED 2026-10-18 (was 15px) - a re-approval, NOT a weakening.
        # The owner asked twice for a page that is easy to read on a phone, and
        # 13px body copy under a 15px headline made the text he actually reads
        # the smallest thing on the page. Headline 15 -> 16px and body 13 ->
        # 16px, so the step between headline and body is now carried by WEIGHT
        # and COLOUR instead of by size, and nothing he must read is smaller
        # than the headline above it. Both values are still pinned exactly.
        self.assertEqual(story_title, "16px")
        self.assertEqual(section_label, "11px")

        # And the hierarchy the design asks for, as strict inequalities.
        self.assertGreater(page_title, story_title,
                           "the page title must outrank a story title")
        self.assertGreater(story_title, section_label,
                           "a story title must outrank its section label")

        # Mobile-only means fixed sizes: no viewport-dependent type can creep
        # back in, because there is no desktop layout to be correct at.
        self.assertNotIn("clamp(", self.page)
        self.assertNotIn("vw", self.page)

        # The supporting scale, so a later edit cannot quietly re-inflate the
        # supporting text while leaving the three headings correct.
        self.assertEqual(css_size("your-day-label"), "10px")
        self.assertEqual(css_size("card-meta"), "11px")
        self.assertEqual(css_size("chip"), "9px")
        self.assertEqual(css_size("your-day-temp"), "12px")
        # RE-APPROVED 2026-10-18 (was 13px): reading text, raised to the same
        # 16px as the headline it sits under, for the reason stated in the
        # pinned-scale test above.
        self.assertEqual(css_size("card-summary"), "16px")

        # `.section-status` is on this list now, and that is the point of it.
        # It used to pin 0.95rem = 15.2px, i.e. LARGER than the 15px story
        # titles it annotates, so it was deliberately absent from a list whose
        # subject is "nothing here outranks the content". Pinning the exact new
        # value keeps the regression from returning through the back door; the
        # GENERAL invariant is the next test, and it is the one that holds even
        # if this number is later re-approved.
        self.assertEqual(css_size("section-status"), "11px")
        # Same letter-spacing as the section label, so the label and the status
        # line underneath it read as one pair rather than as two unrelated bits
        # of furniture.
        status_rule = re.search(r"\.section-status \{([^}]*)\}", self.page).group(1)
        label_rule = re.search(r"\.section-header \{([^}]*)\}", self.page).group(1)
        self.assertIn("letter-spacing: 1px", status_rule)
        self.assertIn("letter-spacing: 1px", label_rule)

        # The weather glyph is 15px because it is an inline SVG, so its size is
        # in the markup rather than in a font-size. Asserted here for the same
        # reason: it is an approved dimension that CSS cannot express.
        self.assertIn('<svg class="weather-icon" width="15" height="15"', self.page)

    # ------------------------------------------------------------------
    # THE GENERAL INVARIANT. The test above pins NUMBERS; this one pins the
    # RULE the numbers came from, because a pinned number only fails when
    # somebody edits that exact number.
    # ------------------------------------------------------------------

    #: Elements whose job is to annotate a section or a story - status, meta,
    #: deadlines, chips, health lines, the footer, the section label itself.
    #: None of them is content, so none of them may outrank the content.
    #: `content_only` is what is deliberately NOT here: `.card-title` (the
    #: anchor), `.card-summary` / `.card-foryou` / `.card-links` (the story's
    #: own words) and `.your-day li` (a headline in the summary card).
    #:
    #: WRITTEN AS FULL SELECTORS, not bare class names, because `footer` is an
    #: ELEMENT selector and asking for `.footer` finds nothing. The first
    #: version of this list held bare names and failed on that alone - which is
    #: the shape of a test that is green for the wrong reason.
    STATUS_AND_META = (
        ".section-status", ".section-header", ".card-meta", ".card-deadline",
        ".chip", ".badge", ".hero-cutoff", ".hero-cutoff-unverified",
        ".health-line", ".health-note", ".health-down", "footer",
        ".your-day-label", ".weather-label", ".back-link",
    )

    def test_no_metadata_text_outranks_the_story_titles(self):
        """THE RULE, NOT THE NUMBERS: metadata never outranks the stories it
        annotates. A status line is a fact about a SECTION; if it is the largest
        text in the section, the reader is reading furniture.

        This is a GENERAL sweep, and that is what makes it stronger than the
        pinned-scale test above in three concrete ways:

          1. It DISCOVERS the rules. Every `<selector> { ... font-size: ... }` in
             the page's stylesheet is parsed and measured, so a status element
             that did not exist when this test was written is covered the day it
             is added - which a list of named selectors cannot do.
          2. It MEASURES, rather than string-matching. Sizes are resolved to
             pixels through a unit converter, so `0.95rem` (15.2px) is caught
             even though it is a different unit from the 16px it outranks, and a
             future `1.1rem` is caught too. Any size the converter cannot resolve
             (a `clamp()`, a `vw`) is a FAILURE, not a skip: a unit the test
             cannot compute must never become a rule the test silently ignores.
          3. It states the bound STRICTLY for status/meta (`< 16px`), not
             loosely (`<= 16px`). "Not larger" is how this page got here - a
             status line at exactly 16px would read as equal, and equal is not
             subordinate.

        The only element permitted to exceed a story title is the page title
        itself, which is what a title is for.
        """
        style = re.search(r"<style>(.*?)</style>", self.page, re.S)
        self.assertIsNotNone(style, "the page has no stylesheet")
        # CSS comments first. Without this a rule regex will happily match a
        # selector that begins inside the comment block above .section-status
        # and report the comment as a selector - which is a real trap here,
        # because that comment is long and mentions font sizes.
        css = re.sub(r"/\*.*?\*/", "", style.group(1), flags=re.S)

        def to_px(value):
            """Resolve a CSS length to px, or None if it cannot be computed."""
            v = value.strip().lower()
            for suffix, factor in (("px", 1.0), ("rem", 16.0), ("pt", 96.0 / 72.0)):
                if v.endswith(suffix):
                    try:
                        return float(v[:-len(suffix)]) * factor
                    except ValueError:
                        return None
            return None

        measured = {}
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            declared = re.search(r"font-size:\s*([^;}]+)", rule.group(2))
            if not declared:
                continue
            selector = " ".join(rule.group(1).split())
            pixels = to_px(declared.group(1))
            self.assertIsNotNone(
                pixels,
                "%s declares font-size %r, which is not a length this test can "
                "resolve to px - a status size the invariant cannot measure is "
                "a status size the invariant must not accept"
                % (selector, declared.group(1).strip()))
            self.assertNotIn(selector, measured,
                             "%s declares font-size twice" % selector)
            measured[selector] = pixels

        self.assertIn(".card-title", measured, "the anchor rule is missing")
        story_title = measured[".card-title"]
        # RE-APPROVED 2026-10-18 (was 15.0) - the anchor moved on purpose, and
        # the sweep below moved with it: nothing but the page title may exceed
        # this, and the 16px body copy sits AT it rather than above it.
        self.assertEqual(story_title, 16.0,
                         "the story title anchor moved; re-approve the scale")

        # (a) THE NAMED CONTRACT. Every status/meta element the page renders
        # today, each strictly below the story titles it annotates.
        for selector in self.STATUS_AND_META:
            self.assertIn(selector, measured,
                          "%s no longer declares a font-size; the type scale "
                          "gained a hole here" % selector)
            self.assertLess(
                measured[selector], story_title,
                "%s is %.1fpx and outranks the %.1fpx story titles - status and "
                "meta text must be visually subordinate to the content"
                % (selector, measured[selector], story_title))

        # (b) THE SWEEP. Nothing anywhere in the stylesheet outranks a story
        # title except the page title. This is the part that generalises: a
        # NEW element added above 16px fails here with no change to this file.
        allowed_above = {".topbar-title"}
        for selector, pixels in sorted(measured.items()):
            if pixels > story_title:
                self.assertIn(
                    selector, allowed_above,
                    "%s renders at %.1fpx, above the %.1fpx story titles. Only "
                    "the page title may outrank a story; anything else doing it "
                    "is the metadata-outranks-content defect."
                    % (selector, pixels, story_title))

        # (c) The sweep is only worth anything if it actually swept something.
        # Without this floor the test would pass on a stylesheet that had lost
        # every font-size declaration, which is the shape of a false green.
        self.assertGreaterEqual(
            len(measured), 20,
            "only %d rules with a font-size were found - the sweep is not "
            "looking at the real stylesheet" % len(measured))

    def test_story_titles_carry_the_approved_leading(self):
        """16px/600 at line-height 1.35, which is what makes a three-line
        headline readable at 390px instead of a grey block."""
        rule = re.search(r"\.card-title \{([^}]*)\}", self.page).group(1)
        self.assertIn("font-weight: 600", rule)
        self.assertIn("line-height: 1.35", rule)

    # -- the empty-label bug class, re-checked on this page -----------------

    def test_for_you_renders_for_the_one_story_that_has_one_and_nobody_else(self):
        """The known bug class in this repo is an empty bold label. Exactly one
        story here carries a for_you, so exactly one label may ship."""
        self.assertEqual(self.page.count('class="card-foryou"'), 1)
        self.assertIn("leave before 16:00, the road floods", self.page)
        self.assertEqual(self.page.count("<strong>For you:</strong>"), 1)
        # No card carries a label with nothing after it.
        for card in re.findall(r'<article class="card">(.*?)</article>', self.page, re.S):
            if 'class="card-foryou"' in card:
                self.assertNotIn('<strong>For you:</strong></p>', card)


# ---------------------------------------------------------------------------
# 8. One clock
# ---------------------------------------------------------------------------

class TestSingleClock(GoldenRenderMixin, unittest.TestCase):
    def test_kathmandu_now_matches_independent_utc_plus_545(self):
        npt = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) \
            + datetime.timedelta(hours=5, minutes=45)
        got = datetime.datetime.strptime(render_digest.kathmandu_now_str(),
                                          "%Y-%m-%d %H:%M")
        self.assertLess(abs((got - npt).total_seconds()), 120,
                        "kathmandu_now_str is not NPT: %s vs %s"
                        % (render_digest.kathmandu_now_str(), npt))

    def test_footer_uses_that_clock_and_is_labelled(self):
        footer = re.search(r"Generated by Personal News Informer · (.*?)</p>|"
                           r"Generated by Personal News Informer · (.*?)\n", self.page)
        self.assertIsNotNone(footer, "footer clock not found")
        stamp = (footer.group(1) or footer.group(2)).strip()
        self.assertTrue(stamp.endswith("NPT"),
                        "footer clock must be labelled NPT, got %r" % stamp)
        got = datetime.datetime.strptime(stamp[:-4].strip(), "%Y-%m-%d %H:%M")
        now = datetime.datetime.strptime(render_digest.kathmandu_now_str(),
                                         "%Y-%m-%d %H:%M")
        self.assertLess(abs((got - now).total_seconds()), 300,
                        "footer clock %s disagrees with the NPT clock %s" % (got, now))

    def test_no_naive_local_clock_remains_in_the_renderer(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "render_digest.py"),
                     encoding="utf-8") as fh:
            body = fh.read()
        self.assertNotIn("datetime.datetime.now()", body,
                         "the naive local clock is what produced two clocks")
        self.assertIn("KATHMANDU_UTC_OFFSET", body)

    def test_hero_and_footer_agree_within_the_minute(self):
        """The hero cutoff is the digest's data cutoff; the footer is 'now'.
        Rendered back to back they must not be 5h45m apart."""
        hero = re.search(r'<p class="hero-cutoff[^"]*">([^<]+)</p>', self.page)
        self.assertIsNotNone(hero)
        now = datetime.datetime.strptime(render_digest.kathmandu_now_str(),
                                         "%Y-%m-%d %H:%M")
        stamp = re.search(r"Generated by Personal News Informer · (.*?)</footer>",
                          self.page, re.S)
        self.assertIsNotNone(stamp)
        self.assertIn(now.strftime("%H:%M"), stamp.group(1),
                      "footer clock is not the same NPT clock the hero uses")


if __name__ == "__main__":
    unittest.main()
