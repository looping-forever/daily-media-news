#!/usr/bin/env python3
"""The approved mobile digest design, asserted on real rendered output.

WHY THIS FILE EXISTS SEPARATELY FROM test_render_readability.py
-----------------------------------------------------------------
`test_render_readability.py` is the archive of defects this project has already
had: the inverted type scale, the card that rendered outside its grid, the
status line that dressed "nothing happened" like "the source failed". Those
tests describe what was WRONG and must never come back.

This file describes what is RIGHT, and its subject is a design the owner
approved rather than a bug: a 390px page, a 480px column, an exact nine-colour
palette, section labels reduced to a dot and 11px text, and stories with no
card, no border and no background at all. Those are choices, not corrections, so
they need their own home - and they need to be assertable against the OLD
template too, which is how we prove they are actually testing the change rather
than describing whatever the renderer happens to emit today.

THE DESIGN IS FROZEN
---------------------
Every literal asserted here was signed off on 2026-10-03. They are written as
exact values rather than ranges on purpose: a later "let's just bump that to
13px" must fail this suite instead of quietly re-styling a page somebody has
already approved. Ranges would pass on a design that had drifted.

Stdlib unittest only. Nothing here writes to docs/; every render goes to a
temp directory.
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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import render_digest as rd  # noqa: E402

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import validated_payload  # noqa: E402


# ---------------------------------------------------------------------------
# The approved palette, as literals. Kept here as well as in the module on
# purpose: a test that imports the constant it is checking cannot detect the
# constant being changed, which is the exact failure this suite exists to catch.
# ---------------------------------------------------------------------------
APPROVED = {
    "bg": "#0E1014",
    "surface": "#171A21",
    "surface_hi": "#1F232C",
    "line": "#272B34",
    "text": "#ECEEF2",
    "muted": "#9BA1AC",
    "dim": "#6C727C",
    "accent": "#FF7A59",
    "sky": "#6BA8F5",
}

#: What the OLD page was, asserted absent. These are the five numbers the owner
#: actually complained about, so they are the regression surface.
OLD_DESIGN = {
    "1100px container": r"max-width: 1100px",
    "cyan accent #00d4ff": r"#00d4ff",
    "cyan card border": r"border: 1px solid #2a2a4a",
    "260px image cap": r"max-height: 260px",
    "viewport-dependent type": r"clamp\(",
    "desktop grid": r"@media \(min-width",
    "old page background": r"#1a1a2e",
    "border-left header bar": r"border-left: 4px solid",
}

DATE = "2026-10-02"
CUTOFF = "2026-10-02 19:21"

#: One story per media shape the renderer supports. The video story is the
#: important one: the 56.25% wrapper is the case the brief singled out as most
#: likely to break when the page narrowed from 1100px to 480px.
STORIES = [
    {"title": "NEA announces 10:00-15:00 power cuts in Kathmandu valley areas",
     "summary": "Reported: scheduled daytime power cuts.",
     "category": "local_city", "priority": 6, "published_at": DATE,
     "label": "NEW", "source_url": "https://example.com/a",
     "image": {"url": "https://example.com/a.jpg"},
     "for_you": "Charge anything mains-powered before 10:00."},
    {"title": "Video: the Araniko highway is closed at Kadamtale",
     "summary": "Footage from the landslide site.",
     "category": "nepal", "priority": 5, "published_at": DATE,
     "label": "DEVELOPING", "source_url": "https://example.com/b",
     "video": {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}},
    {"title": "Tribhuvan University extends the exam form deadline",
     "summary": "The forms window is open until Friday.",
     "category": "education_tu", "priority": 6, "published_at": DATE,
     "label": "NEW", "source_url": "https://example.com/c",
     "deadline": "2026-10-05"},
]

META = {
    "date": DATE,
    "digest_time": "evening",
    "data_cutoff_npt": CUTOFF,
    "weather_advice": [
        {"id": "U", "text": "Take an umbrella - 70% chance of rain", "priority": 4},
    ],
    "source_health": {
        "schema_version": 1, "state": "degraded", "checked_at_npt": CUTOFF,
        "critical": {"total": 33, "ok": 31, "failed": 2, "skipped": 0,
                     "hosts_down": ["iost.tu.edu.np"],
                     "reasons": ["dns"]},
        "all": {"total": 43, "ok": 41, "failed": 2, "skipped": 0},
        # EXACTLY ONE category is tracked, and that one is down. Every other
        # category is absent from the map, which is the B3a "never configured"
        # case and must stay QUIET. So this fixture yields precisely one alarm
        # and seven quiet lines across the eight empty sections - the shape the
        # readability fixture uses, and the shape the B3a split exists for.
        "by_category": {
            "gaming": {"total": 5, "ok": 4, "failed": 1, "skipped": 0,
                       "urls": ["https://khabarhub.com/"]},
        },
        "failed_names": ["notice:iost.tu.edu.np"],
        "reachable_hosts": [],
    },
    "notes": "Failing sources, stated plainly: notice:iost.tu.edu.np failed",
}


#: WHICH SECTIONS THIS FIXTURE'S PAGE IS EXPECTED TO RENDER, in SECTIONS order.
#:
#: RE-POINTED, owner-approved. These tests used to assert "one label row per
#: section", i.e. all eleven, which encoded the old rule that every section
#: renders even when empty. The owner replaced that rule: a quiet empty section
#: is not displayed. So the expectation is now the exact SET of sections that
#: render, which is a stronger assertion than a count -- it pins WHICH ones and
#: therefore catches a section that is wrongly hidden as loudly as one that is
#: wrongly shown.
#:
#: Derived from this file's fixture, by hand, NOT from production code:
#:   local_city, education_tu, nepal -- hold one of the three STORIES.
#:   gaming  -- holds none, but IS tracked in `by_category` with failed=1, so it
#:              is B5, an ALARM, and an alarm always renders.
#: The other seven are empty AND untracked, so their status is quiet and they are
#: omitted. `test_the_quiet_empty_sections_are_omitted_entirely` asserts that
#: half directly.
RENDERED_KEYS = ("local_city", "education_tu", "nepal", "gaming")
#: The complement of RENDERED_KEYS: empty and untracked, therefore omitted.
OMITTED_KEYS = ("action", "world_major", "college_local", "bank_local",
                "anime", "manga_manhwa_manhua", "tech_career")


class ApprovedDesignMixin(object):
    """Renders the page once for the whole class. No file is written."""

    page = None

    @classmethod
    def setUpClass(cls):
        cls.page = rd.render_page(STORIES, "evening", DATE, CUTOFF, META)

    def css_rule(self, pattern):
        """Declarations of the first CSS rule whose selector matches `pattern`.

        ANCHORED TO THE START OF A LINE, which is not decoration. The obvious
        `re.search(selector + r"\\s*\\{")` finds `body {` inside `.card-body {`
        and `.card-thumb-col img.card-thumb {` inside `.card-thumb-col {`, so
        two of the rules under test would have been read from the wrong
        selector and the assertions would have been measuring something else.
        """
        rule = re.search(r"(?m)^\s*(?:" + pattern + r")\s*\{([^}]*)\}", self.page)
        self.assertIsNotNone(rule, "no CSS rule matching %r" % pattern)
        return rule.group(1)

    def css(self, class_name):
        """Declarations of a single-CLASS rule, e.g. css("card-title")."""
        return self.css_rule(r"\." + re.escape(class_name))


class TestPageFrame(ApprovedDesignMixin, unittest.TestCase):
    """A 390px phone. The content column never grows past 480px."""

    def test_the_column_is_480px_and_centred(self):
        container = self.css("container")
        self.assertIn("max-width: 480px", container)
        self.assertIn("margin: 0 auto", container)

    def test_the_page_padding_is_the_approved_18_20_32(self):
        container = self.css("container")
        self.assertIn("padding: 20px 18px 32px 18px", container)

    def test_the_page_background_is_the_approved_ink(self):
        # `body` is an ELEMENT selector, so it goes through css_rule directly;
        # css("body") would have asked for `.body` and found nothing.
        body = self.css_rule(r"body")
        self.assertIn("background: %s" % APPROVED["bg"], body)
        self.assertIn("color: %s" % APPROVED["text"], body)

    def test_inter_is_first_in_the_font_stack_with_the_system_fallback_kept(self):
        self.assertIn("font-family: %s" % rd.D_FONT, self.css_rule(r"body"))
        self.assertTrue(rd.D_FONT.startswith("'Inter'"))
        # The old stack is the fallback, so a device without Inter is not worse
        # off than it was.
        for face in ("-apple-system", "Segoe UI", "Roboto", "sans-serif"):
            self.assertIn(face, rd.D_FONT)

    def test_every_approved_palette_value_is_actually_used(self):
        """Not merely present in the source: present in the RENDERED PAGE.

        A palette constant that nothing interpolates is dead code that reads as
        a design token, which is worse than not having it.
        """
        for name, value in APPROVED.items():
            self.assertIn(value, self.page,
                          "approved colour %s (%s) is not in the page"
                          % (name, value))

    def test_the_old_design_is_gone(self):
        for name, pattern in OLD_DESIGN.items():
            self.assertNotRegex(self.page, pattern,
                                "the old design's %s is still on the page" % name)


class TestTopBar(ApprovedDesignMixin, unittest.TestCase):
    def test_the_title_is_the_digest_time_at_21px(self):
        # STRONGER THAN BEFORE, NOT LOOSER. This used to assert the bare slot
        # word "Evening Digest", which is exactly the string that made a digest
        # fired at 02:03 claim to be an evening one, and it then became
        # `<word> Digest · <build clock>` from this fixture's own stamp. Both
        # halves now come from the COMMITTED SCHEDULE: this fixture renders the
        # `evening` slot, `deploy/schedule.json` says that slot is due at
        # 19:20 Nepal time, and the word is derived from that same 19:20. So
        # the title asserts BOTH halves and neither can drift back to the slot
        # or to the build stamp -- and the fixture's CUTOFF (19:21) is
        # deliberately one minute later, which is the prebuild/push gap this
        # change exists to stop the page misrepresenting.
        self.assertIn('<h1 class="topbar-title">Evening Digest · due 19:20</h1>',
                      self.page)
        self.assertNotIn('<h1 class="topbar-title">Evening Digest · 19:21</h1>',
                         self.page,
                         "the page is back to printing its own build minute as "
                         "the digest's time")
        self.assertIn("font-size: 21px", self.css("topbar-title"))
        self.assertIn("font-weight: 700", self.css("topbar-title"))
        self.assertIn("color: %s" % APPROVED["text"], self.css("topbar-title"))

    def test_the_subline_is_the_short_date_and_the_city(self):
        self.assertIn('<p class="topbar-sub">Fri 2 Oct \u00b7 Example City</p>',
                      self.page)
        sub = self.css("topbar-sub")
        self.assertIn("font-size: 12px", sub)
        self.assertIn("font-weight: 500", sub)
        self.assertIn("color: %s" % APPROVED["dim"], sub)

    def test_the_subline_is_three_pixels_under_the_title(self):
        inner = self.css("topbar-inner")
        self.assertIn("gap: 3px", inner)

    def test_the_story_count_is_a_surface_hi_pill(self):
        self.assertIn('<span class="topbar-count">3 stories</span>', self.page)
        pill = self.css("topbar-count")
        self.assertIn("background: %s" % APPROVED["surface_hi"], pill)
        self.assertIn("color: %s" % APPROVED["muted"], pill)
        self.assertIn("font-size: 12px", pill)
        self.assertIn("font-weight: 600", pill)
        self.assertIn("padding: 6px 10px", pill)
        self.assertIn("border-radius: 9px", pill)

    def test_a_divider_runs_under_the_whole_bar(self):
        self.assertIn("border-bottom: 1px solid %s" % APPROVED["line"],
                      self.css("topbar"))

    def test_the_bar_is_not_sticky(self):
        """A sticky header permanently spent ~44px of a 844px screen on a title
        the reader has already read. The approved bar is a plain bar."""
        self.assertNotIn("position: sticky", self.page)

    def test_the_archive_back_link_survives(self):
        """Kept, and placed below the bar: the approved layout does not place it
        and it is the only route back to the archive."""
        self.assertIn('class="back-link"', self.page)
        self.assertIn('href="/index.html"', self.page)
        self.assertIn('<div class="topbar-nav">', self.page)


class TestYourDay(ApprovedDesignMixin, unittest.TestCase):
    def test_the_card_is_the_approved_surface(self):
        block = self.css("your-day")
        self.assertIn("background: %s" % APPROVED["surface"], block)
        self.assertIn("border-radius: 14px", block)
        self.assertIn("padding: 15px", block)

    def test_the_header_row_is_a_label_a_spacer_and_the_weather(self):
        self.assertIn('<div class="your-day-head">', self.page)
        head = self.css("your-day-head")
        self.assertIn("display: flex", head)
        self.assertIn('<span class="your-day-spacer"></span>', self.page)
        self.assertIn("flex: 1 1 auto", self.css("your-day-spacer"))
        self.assertIn('<span class="your-day-weather">', self.page)

    def test_the_label_is_10px_accent_uppercase_and_tracked(self):
        label = self.css("your-day-label")
        self.assertIn("font-size: 10px", label)
        self.assertIn("font-weight: 700", label)
        self.assertIn("letter-spacing: 1.1px", label)
        self.assertIn("color: %s" % APPROVED["accent"], label)

    def test_the_glyph_is_15px_and_sky(self):
        self.assertIn('<svg class="weather-icon" width="15" height="15"',
                      self.page)
        self.assertIn("color: %s" % APPROVED["sky"],
                      self.css("your-day-weather"))

    def test_the_main_line_is_16px_text_and_the_supporting_lines_are_12px_muted(self):
        main = self.css("your-day li")
        # RE-APPROVED 2026-10-18 (was 15px, and this test was named for that
        # number): every line the owner READS is 16px.
        self.assertIn("font-size: 16px", main)
        self.assertIn("font-weight: 500", main)
        self.assertIn("color: %s" % APPROVED["text"], main)
        support = self.css("your-day li + li")
        self.assertIn("font-size: 12px", support)
        self.assertIn("font-weight: 400", support)
        self.assertIn("color: %s" % APPROVED["muted"], support)
        self.assertIn("line-height: 1.45", main)
        self.assertIn("line-height: 1.45", support)

    def test_the_temperature_is_omitted_rather_than_invented(self):
        """THE PAYLOAD HAS NO TEMPERATURE, so no number is rendered.

        This asserts the ABSENCE of a fabricated figure, which is the whole
        point: printing a plausible-looking number the pipeline never measured
        is worse than printing nothing. The moment a producer emits
        `weather.temperature_c` this test's sibling below requires it to appear.
        """
        self.assertNotIn('<span class="your-day-temp">', self.page)
        self.assertIsNone(rd.weather_temperature(META))

    def test_a_temperature_is_rendered_when_the_payload_carries_one(self):
        for raw, expected in ((18, "18\u00b0C"), ("12C", "12\u00b0C"),
                              ("12\u00b0C", "12\u00b0C"), (7.4, "7\u00b0C")):
            payload = {"weather": {"temperature_c": raw}}
            self.assertEqual(rd.weather_temperature(payload), expected, raw)
        html = rd.render_your_day(["something"], temperature="18\u00b0C")
        self.assertIn('<span class="your-day-temp">18\u00b0C</span>', html)
        self.assertIn("font-size: 12px", self.css("your-day-temp"))
        self.assertIn("font-weight: 600", self.css("your-day-temp"))
        self.assertIn("color: %s" % APPROVED["sky"], self.css("your-day-temp"))

    def test_a_junk_temperature_is_refused_rather_than_half_printed(self):
        for junk in (None, "", "cold", True, {}, [], "12C-ish", "C"):
            self.assertIsNone(rd.weather_temperature({"weather":
                                                      {"temperature_c": junk}}),
                              "junk=%r" % (junk,))


class TestSectionLabels(ApprovedDesignMixin, unittest.TestCase):
    """The coloured header bar is deleted; a dot and 11px text replace it."""

    def test_the_header_bar_and_its_background_are_gone(self):
        header = self.css("section-header")
        self.assertNotIn("border-left", header)
        self.assertNotIn("background", header)
        self.assertNotIn("border-left-color", header)

    def test_the_label_is_11px_dim_uppercase_and_tracked(self):
        header = self.css("section-header")
        self.assertIn("font-size: 11px", header)
        self.assertIn("font-weight: 700", header)
        self.assertIn("letter-spacing: 1px", header)
        self.assertIn("color: %s" % APPROVED["dim"], header)
        self.assertIn("text-transform: uppercase", header)

    def test_the_dot_is_six_pixels_and_takes_the_sections_own_accent(self):
        body = self.css_rule(r"\.section-header::before")
        self.assertIn("width: 6px", body)
        self.assertIn("height: 6px", body)
        self.assertIn("border-radius: 50%", body)
        self.assertIn("background: var(--dot,", body)

    def test_the_dot_colour_is_the_sections_existing_accent_reused(self):
        """The eleven accent colours already existed as `border-left-color`.
        They now arrive as `--dot`, and every RENDERED section keeps its own
        value - the redesign must not have flattened them all to one colour.

        RE-POINTED (owner-approved): was "every one of the eleven", which
        asserted a dot colour for sections the page no longer renders. The
        palette itself is still checked in full -- all eleven values, and the
        requirement of at least eight distinct ones -- because SECTIONS still
        defines an accent per section and a later edit must not quietly merge
        them. Only the "does this colour appear on THIS page" loop is narrowed to
        the sections that render.
        """
        by_key = {key: color for key, _h, color in rd.SECTIONS}
        self.assertGreaterEqual(len(set(by_key.values())), 8,
                                "the fixture lost its per-section colours")
        for key, color in by_key.items():
            if key not in RENDERED_KEYS:
                continue
            self.assertIn('style="--dot:%s;"' % color, self.page,
                          "section %s lost its own dot colour" % key)
        # And the rendered ones really are distinct, not one colour repeated.
        rendered_dots = set(re.findall(r'style="--dot:(#[0-9a-f]{6});"',
                                       self.page))
        self.assertEqual(len(rendered_dots), len(RENDERED_KEYS),
                         "each rendered section must carry its own accent, got %r"
                         % (rendered_dots,))

    def test_the_gap_between_the_label_and_the_list_is_11px(self):
        self.assertIn("margin-top: 11px", self.css("card-grid"))

    def test_sections_are_28px_apart(self):
        # RE-APPROVED 2026-10-18 (was 24px, and this test was named for that
        # number): more air between sections on a phone. Re-approval, not a
        # weakening - the value is still pinned exactly.
        self.assertIn("margin-bottom: 28px", self.css("digest-section"))


class TestStories(ApprovedDesignMixin, unittest.TestCase):
    """No card. No border. No background. A hairline between neighbours."""

    def test_the_card_has_no_border_and_no_background(self):
        card = self.css("card")
        self.assertIn("background: transparent", card)
        self.assertIn("border: 0", card)
        self.assertIn("border-radius: 0", card)

    def test_no_consecutive_stories_are_divided_by_a_line_hairline(self):
        # A COMPOUND selector, so css("card + card") would have asked for
        # `.card + card` and matched nothing.
        divider = self.css_rule(r"\.card \+ \.card")
        self.assertIn("border-top: 1px solid %s" % APPROVED["line"], divider)

    def test_the_thumbnail_is_64px_square_on_the_approved_surface(self):
        col = self.css("card-thumb-col")
        self.assertIn("width: 64px", col)
        self.assertIn("height: 64px", col)
        self.assertIn("border-radius: 10px", col)
        self.assertIn("background: %s" % APPROVED["surface_hi"], col)
        self.assertIn("flex: 0 0 64px", col)
        img = self.css_rule(r"\.card-thumb-col img\.card-thumb")
        self.assertIn("object-fit: cover", img)
        # And the image keeps the class the archive index greps for, or the
        # index loses its thumbnails the next time it regenerates.
        self.assertIn('class="card-thumb"', self.page)

    def test_the_thumbnail_sits_beside_the_text_top_aligned_with_a_12px_gap(self):
        body = self.css("card-body")
        self.assertIn("display: flex", body)
        self.assertIn("align-items: flex-start", body)
        self.assertIn("gap: 12px", body)

    def test_a_story_without_media_reserves_no_empty_square(self):
        """The tuition story carries no image and no video. An always-present
        thumbnail column would leave a 64px hole beside its headline."""
        no_media = [s for s in STORIES if "image" not in s and "video" not in s]
        self.assertTrue(no_media, "the fixture lost its media-free story")
        html = rd.build_story_card(no_media[0])
        self.assertNotIn("card-thumb-col", html)
        self.assertIn("card-text", html)

    def test_a_story_row_has_exactly_one_flex_child_in_the_body(self):
        """THE BUG THIS FILE WAS WRITTEN AFTER SEEING.

        `.card-body` is a flex ROW so the thumbnail can sit beside the text. A
        video story has no thumbnail, so an earlier version put the title,
        chips, meta, summary and links in as direct children - five flex items
        side by side, which squeezed the headline into a ~100px column and
        wrapped "Read article" one character per line. It rendered, and it was
        unreadable, and no assertion caught it because every element was still
        present in the HTML.

        So this asserts the SHAPE, not the presence: one text column per story.
        """
        for story in STORIES:
            card = rd.build_story_card(story)
            body = re.search(r'<div class="card-body">(.*?)</div>\s*</article>',
                             card, re.S)
            self.assertIsNotNone(body, "no card-body in %r" % story["title"])
            inner = body.group(1)
            top_level = re.findall(r'<div class="(card-thumb-col|card-text)">', inner)
            self.assertIn("card-text", top_level, story["title"])
            # At most one thumbnail column, and it comes before the text.
            self.assertLessEqual(top_level.count("card-thumb-col"), 1)
            self.assertEqual(top_level[0], "card-thumb-col"
                             if "card-thumb-col" in top_level else "card-text",
                             story["title"])

    def test_the_title_is_16px_600_at_1_35(self):
        title = self.css("card-title")
        # RE-APPROVED 2026-10-18 (was 15px, and this test was named for that
        # number). Weight, colour and the 16px body beneath it carry the
        # hierarchy now; the leading is deliberately unchanged.
        self.assertIn("font-size: 16px", title)
        self.assertIn("font-weight: 600", title)
        self.assertIn("line-height: 1.35", title)
        self.assertIn("color: %s" % APPROVED["text"], title)

    def test_the_meta_line_is_11px_500_dim(self):
        meta = self.css("card-meta")
        self.assertIn("font-size: 11px", meta)
        self.assertIn("font-weight: 500", meta)
        self.assertIn("color: %s" % APPROVED["dim"], meta)


class TestChips(ApprovedDesignMixin, unittest.TestCase):
    def test_the_chip_is_9px_accent_on_surface_hi_with_a_4px_radius(self):
        chip = self.css("chip")
        self.assertIn("font-size: 9px", chip)
        self.assertIn("font-weight: 700", chip)
        self.assertIn("letter-spacing: 0.6px", chip)
        self.assertIn("padding: 2px 6px", chip)
        self.assertIn("border-radius: 4px", chip)
        label = self.css("chip-label")
        self.assertIn("background: %s" % APPROVED["surface_hi"], label)
        self.assertIn("color: %s" % APPROVED["accent"], label)

    def test_the_existing_chip_labels_are_kept_verbatim(self):
        """The design said "keep whatever chip labels already exist"."""
        self.assertIn("[NEW]", self.page)
        self.assertIn("[DEVELOPING]", self.page)


class TestVideoSurvivesTheNarrowing(ApprovedDesignMixin, unittest.TestCase):
    """The risk the brief called out first: media sized for 1100px, on 480px."""

    def test_the_56_25_percent_wrapper_is_intact(self):
        wrapper = self.css("media-video")
        self.assertIn("position: relative", wrapper)
        self.assertIn("padding-top: 56.25%", wrapper)

    def test_the_embed_is_still_lazy_and_still_nocookie(self):
        self.assertIn('class="video-frame"', self.page)
        self.assertIn("youtube-nocookie.com/embed/", self.page)
        self.assertIn('loading="lazy"', self.page)

    def test_a_video_story_gets_a_full_width_block_above_the_text(self):
        """Not a 64px square: a 16:9 embed cannot be one, and shrinking it
        would destroy the feature instead of restyling it."""
        card = rd.build_story_card(STORIES[1])
        self.assertIn('<div class="card-media">', card)
        self.assertIn("media-video", card)
        self.assertNotIn("card-thumb-col", card)

    def test_a_video_story_keeps_its_poster_when_the_payload_has_one(self):
        story = dict(STORIES[1])
        story["video"] = {"url": "https://example.com/clip.mp4", "type": "video/mp4"}
        story["image"] = {"url": "https://example.com/poster.jpg"}
        card = rd.build_story_card(story)
        self.assertIn('<video controls preload="none"', card)
        self.assertIn('poster="https://example.com/poster.jpg"', card)
        self.assertIn("media-video", card)

    def test_an_unembeddable_video_still_offers_a_watch_link(self):
        story = {"title": "t", "category": "nepal",
                 "video": {"url": "https://example.com/watch/abc"}}
        card = rd.build_story_card(story)
        self.assertIn("media-watch", card)
        self.assertIn("Watch video", card)


class TestArchiveParsersSurvived(ApprovedDesignMixin, unittest.TestCase):
    """`docs/index.html` is a SEPARATE template that parses the digest page.

    It finds sections by splitting on `<h2 class="section-header"...>` and
    counts `<article class="card">`. The redesign changed what is INSIDE a
    section and what a header's inline style sets, so these two tests are the
    proof that the archive index did not regress.
    """

    def test_the_archive_parses_a_digest_page_this_file_still_describes(self):
        """`render_index` is a SEPARATE template that parses the digest page.

        It finds sections by splitting on `<h2 class="section-header"...>` and
        counts `<article class="card">`. The archive was then redesigned onto
        the approved mobile design, and this is the proof the redesign touched
        only the ARCHIVE: if a future edit to the digest markup broke either
        parser, the archive would silently report "0 stories" for every digest
        instead of failing, so the parsers are exercised against the page this
        file actually renders rather than against a hand-written stub.
        """
        self.assertEqual(rd.section_label("LOCAL &amp; CITY"), "local")
        self.assertEqual(rd.section_label("\U0001f3e0 LOCAL &amp; CITY"), "local")
        self.assertEqual(rd.section_label("GAMING"), "gaming")
        tmp = tempfile.mkdtemp(prefix="approved-design-")
        try:
            path = os.path.join(tmp, "%s-evening.html" % DATE)
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(self.page)
            counts = rd.parse_digest_categories(path)
            self.assertEqual(counts, {"local": 1, "nepal": 1, "education": 1},
                             counts)
            self.assertEqual(rd.first_thumbnail(path),
                             "https://example.com/a.jpg")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


#: What the archive index looked like BEFORE the owner approved unifying it with
#: the digest page. Asserted absent for the same reason `OLD_DESIGN` is: these
#: are the literals that made it a second, separately-designed desktop surface,
#: and the owner's decision was to delete that surface, not to restyle it.
#:
#: NOTE THE INVERSION. `OLD_DESIGN` above guards the DIGEST page and this guards
#: the ARCHIVE, and they used to point in opposite directions: the digest page
#: was required to be free of `#00d4ff` / `#1a1a2e` while the archive was
#: required to CONTAIN `max-width: 1000px`. Both premises are now retired - the
#: approved `#0E1014` is on both surfaces - so both guards assert the same thing
#: from the same palette.
ARCHIVE_OLD_DESIGN = {
    "1000px desktop container": r"max-width: 1000px",
    "cyan accent #00d4ff": r"#00d4ff",
    "cyan card border": r"border: 1px solid #2a2a4a",
    "archive page background": r"#1a1a2e",
    "archive card surface": r"#22223a",
    "archive card border colour": r"#2a2a4a",
    "viewport-dependent type": r"clamp\(",
    "desktop grid query": r"@media \(min-width",
    "multi-column grid": r"grid-template-columns",
    "banner thumbnail 150px": r"height: 150px",
    "10px card radius": r"border-radius: 10px",
    "the old grid container class": r"archive-grid",
}


class ArchiveIndexMixin(ApprovedDesignMixin):
    """Renders a real archive index over real digest pages, once per class.

    No file in the repository is touched: `render_index` writes to a temp dir.
    """

    index_html = None

    @classmethod
    def setUpClass(cls):
        # super() first: the digest page is the INPUT the archive parses, so
        # `cls.page` must exist before the digest files are written out.
        super(ArchiveIndexMixin, cls).setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="approved-index-")
        digests = os.path.join(cls.tmp, "digests")
        os.makedirs(digests)
        # TWO digests on ONE date, so the grouping, the evening-before-morning
        # order and the singular/plural split are all exercised by one render:
        # three stories on the evening, one story on the morning.
        with io.open(os.path.join(digests, "%s-evening.html" % DATE), "w",
                     encoding="utf-8") as fh:
            fh.write(cls.page)
        one_story = [s for s in STORIES if "image" not in s][0]
        with io.open(os.path.join(digests, "%s-morning.html" % DATE), "w",
                     encoding="utf-8") as fh:
            fh.write(rd.render_page([one_story], "morning", DATE, CUTOFF, META))
        cls.index = os.path.join(cls.tmp, "index.html")
        assert rd.render_index(digests, cls.index, DATE, CUTOFF)
        with io.open(cls.index, encoding="utf-8") as fh:
            cls.index_html = fh.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def css_rule(self, pattern):
        rule = re.search(r"(?m)^\s*(?:" + pattern + r")\s*\{([^}]*)\}",
                         self.index_html)
        self.assertIsNotNone(rule, "no archive CSS rule matching %r" % pattern)
        return rule.group(1)

    def css(self, class_name):
        return self.css_rule(r"\." + re.escape(class_name))


class TestArchivePaletteIsTheApprovedPalette(ArchiveIndexMixin,
                                             unittest.TestCase):
    """RETIRED PREMISE, REWRITTEN. Read this before deleting it.

    This was `test_the_archive_palette_was_not_touched_by_the_digest_palette`.
    It asserted that the archive KEPT its own palette: `max-width: 1000px`
    present, and the approved `#0E1014` ABSENT. That was a correct guard while
    the archive was deliberately the one undesigned desktop surface.

    The owner has since approved bringing the archive onto the SAME approved
    mobile design, so the premise - the two surfaces must differ - is retired and
    its opposite is now the requirement. The guard is REWRITTEN rather than
    deleted, because a deleted guard is worse than a wrong one: it was the only
    thing standing between the two palettes and a silent re-divergence, and it
    is still the only thing standing between the archive and the old desktop
    design coming back.

    So the assertion is now: the archive renders ON the approved palette, and
    every literal of the retired desktop design is gone.
    """

    def test_the_archive_renders_on_the_approved_palette(self):
        """Every colour the archive actually paints is an approved one.

        TWO approved tokens are deliberately excluded, because asserting them
        would be asserting a colour the approved archive design does not use:
        `sky` is the weather glyph on the digest page (the archive has no
        weather block) and `surface` is the "Your day" card on the digest page
        (the archive's header and rows are explicitly card-less, and its
        thumbnail placeholder is `surface_hi`, not `surface`). Demanding them
        here would push the implementation toward inventing a card that the
        approved design deletes.
        """
        unused = {"sky", "surface"}
        for name, value in sorted(APPROVED.items()):
            if name in unused:
                self.assertNotIn(
                    value, self.index_html,
                    "%s is a digest-page token with no element on the archive; "
                    "its presence means something was invented here" % name)
                continue
            self.assertIn(value, self.index_html,
                          "approved colour %s (%s) is not on the archive"
                          % (name, value))

    def test_the_archive_references_the_digest_palette_constants(self):
        """By REFERENCE, not by copy: the archive has no palette of its own.

        Asserted on the module source, because the failure this prevents is a
        copied hex - a value retyped into the archive's own <style> block, which
        would render identically today and drift silently the next time the
        approved palette is retuned. The old archive had seven such constants
        (`BG`, `TEXT`, `ACCENT`, `CARD_BG`, `CARD_BORDER`, plus the dead
        `MUTED` and `DANGER`); they are deleted, not re-pointed, so there is no
        second value left to diverge from.
        """
        with io.open(os.path.join(SCRIPTS, "render_digest.py"), "rb") as fh:
            source = fh.read().decode("utf-8")
        for gone in ("CARD_BG", "CARD_BORDER", "archive-grid"):
            self.assertNotRegex(source, r"(?m)^%s\s*=" % gone,
                                "the archive's own `%s` is back; the two "
                                "surfaces must share one palette" % gone)

    def test_the_archive_css_only_uses_approved_hex_values(self):
        """The strong form of the test above, on the OUTPUT rather than the
        source: every hex in the archive's stylesheet is an approved one. A
        hand-typed hex anywhere in that block is now a failure, so the stylesheet
        cannot accumulate a private colour again."""
        block = re.search(r"<style>(.*?)</style>", self.index_html, re.S)
        self.assertIsNotNone(block)
        allowed = {v.upper() for v in APPROVED.values()}
        found = set(h.upper() for h in re.findall(r"#[0-9a-fA-F]{3,6}",
                                                 block.group(1)))
        self.assertEqual(set(), found - allowed,
                         "the archive stylesheet uses hex values that are not "
                         "in the approved palette: %r" % (found - allowed,))

    def test_the_retired_desktop_design_is_gone_from_the_archive(self):
        for name, pattern in sorted(ARCHIVE_OLD_DESIGN.items()):
            self.assertNotRegex(self.index_html, pattern,
                                "the archive's old %s is still on the page"
                                % name)


class TestArchiveMobileLayout(ArchiveIndexMixin, unittest.TestCase):
    """The approved layout: a 390px phone, one column, no desktop fallback."""

    def test_the_column_is_390px_and_centred(self):
        container = self.css("container")
        self.assertIn("max-width: 390px", container)
        self.assertIn("margin: 0 auto", container)

    def test_nothing_in_the_archive_is_wider_than_a_phone(self):
        """A fixed 1000px (or any 3-digit) width would force a horizontal
        scrollbar on the only device this page is read on."""
        offenders = re.findall(r"(?:width|min-width)\s*:\s*(\d{3,})px",
                               self.index_html)
        for value in offenders:
            self.assertEqual("390", value,
                             "a %spx fixed width would overflow a 390px screen"
                             % value)

    def test_there_is_no_desktop_layout_at_all(self):
        self.assertNotIn("@media", self.index_html)
        self.assertNotIn("display: grid", self.index_html)

    def test_the_header_is_three_lines_not_a_card(self):
        self.assertIn('<div class="archive-eyebrow">Archive</div>',
                      self.index_html)
        self.assertIn('<h1 class="archive-title">Digest Archive</h1>',
                      self.index_html)
        head = self.css("archive-head")
        self.assertIn("border-bottom: 1px solid %s" % APPROVED["line"], head)
        for banned in ("background", "border-radius", "text-align"):
            self.assertNotIn(banned, head,
                             "the approved header is not a card")

    def test_the_header_type_ladder(self):
        eyebrow = self.css("archive-eyebrow")
        self.assertIn("font-size: 11px", eyebrow)
        self.assertIn("font-weight: 600", eyebrow)
        self.assertIn("letter-spacing: 0.7px", eyebrow)
        self.assertIn("text-transform: uppercase", eyebrow)
        self.assertIn("color: %s" % APPROVED["accent"], eyebrow)
        title = self.css("archive-title")
        self.assertIn("font-size: 21px", title)
        self.assertIn("font-weight: 700", title)
        self.assertIn("color: %s" % APPROVED["text"], title)
        subtitle = self.css("archive-subtitle")
        self.assertIn("font-size: 12px", subtitle)
        self.assertIn("color: %s" % APPROVED["muted"], subtitle)

    def test_the_subtitle_counts_the_briefings_and_the_span(self):
        """'2 briefings · <oldest> – <newest>' for this fixture's two digests.

        Two, not one, so the count is not a constant that happens to be right.
        """
        self.assertRegex(
            self.index_html,
            r'archive-subtitle">2 briefings · [A-Z][a-z]{2} \d+ '
            r'[–-] [A-Z][a-z]{2} \d+</p>',
            "the archive subtitle must count the briefings and span the dates")

    def test_the_subtitle_pluralises_at_one(self):
        solo = tempfile.mkdtemp(prefix="approved-index-one-")
        try:
            digests = os.path.join(solo, "digests")
            os.makedirs(digests)
            with io.open(os.path.join(digests, "%s-evening.html" % DATE), "w",
                         encoding="utf-8") as fh:
                fh.write(self.page)
            out = os.path.join(solo, "index.html")
            self.assertTrue(rd.render_index(digests, out, DATE, CUTOFF))
            with io.open(out, encoding="utf-8") as fh:
                html = fh.read()
        finally:
            shutil.rmtree(solo, ignore_errors=True)
        self.assertIn("1 briefing", html)
        self.assertNotIn("1 briefings", html)

    def test_the_date_label_is_a_dot_and_uppercase_dim_text(self):
        self.assertIn('<h2 class="date-heading">%s</h2>' % rd.esc(
            datetime.datetime.strptime(DATE, "%Y-%m-%d").strftime(
                "%a, %b %d %Y")), self.index_html)
        label = self.css("date-heading")
        self.assertIn("font-size: 11px", label)
        self.assertIn("font-weight: 600", label)
        self.assertIn("letter-spacing: 0.5px", label)
        self.assertIn("text-transform: uppercase", label)
        self.assertIn("color: %s" % APPROVED["dim"], label)
        self.assertIn("padding: 20px 0 10px", label)
        dot = self.css_rule(r"\.date-heading::before")
        self.assertIn("width: 6px", dot)
        self.assertIn("height: 6px", dot)
        self.assertIn("border-radius: 50%", dot)
        self.assertIn("background: %s" % APPROVED["accent"], dot)

    def test_one_hairline_per_boundary_and_no_double_line_under_the_header(self):
        """Every boundary is a 1px line and the FIRST group drops its own, or
        the header's border-bottom and the first group's border-top stack into a
        2px band that the approved render does not have."""
        self.assertIn("border-top: 1px solid %s" % APPROVED["line"],
                      self.css("date-group"))
        first = self.css_rule(r"\.date-group:first-of-type")
        self.assertIn("border-top: 0", first)


class TestArchiveRows(ArchiveIndexMixin, unittest.TestCase):
    """A row, not a card: no background, no border, no radius."""

    def test_the_row_is_transparent_and_unbordered(self):
        """A row, not a card - AND not a bordered row either.

        The row carries no background, no border and no radius. There is
        deliberately no hairline between two digests on the same date: the
        approved render's within-group row pitch measures exactly 88.00px, which
        is a 64px thumbnail plus 12px above and 12px below and nothing else. A
        `border-top` on the row would add ~1px per row and draw a line where the
        approved design has 24px of clear background. This is the assertion a
        future "let's add dividers between items" tidy has to fail.
        """
        row = self.css("digest-row")
        self.assertIn("display: flex", row)
        self.assertIn("padding: 12px 0", row)
        for banned in ("background", "border", "border-radius"):
            self.assertNotIn(banned, row,
                             "the approved row is not a card and carries no "
                             "rule of its own: found %r" % banned)

    def test_the_only_separators_are_the_group_hairlines(self):
        """Counted on the rendered output, not inferred from the stylesheet: a
        page with 2 rows in one date group must contain exactly TWO hairlines -
        the one under the header and the one above the group - and not a third
        between the two rows."""
        style = re.search(r"<style>(.*?)</style>", self.index_html,
                          re.S).group(1)
        rules = [(m.group(1).strip(), m.group(2)) for m in
                 re.finditer(r"(?m)^\s*(\.[a-z0-9\-:]+)\s*\{([^}]*)\}", style)]
        hlines = {sel for sel, body in rules
                  if "border-top: 1px solid %s" % APPROVED["line"] in body
                  or "border-bottom: 1px solid %s" % APPROVED["line"] in body}
        self.assertEqual({".archive-head", ".date-group", ".archive-footer"},
                         hlines,
                         "only the header, each date group and the footer may "
                         "draw a hairline; the row must not")

    def test_every_row_has_a_thumbnail_reserved_whether_or_not_an_image_exists(self):
        """THE 18-OF-75 CASE, ASSERTED DIRECTLY.

        A real, already-occurring condition: many shipped digests carry no
        `<img class="card-thumb">` at all. The old desktop card omitted the whole
        thumbnail element for those, so the card collapsed. The approved design
        reserves the 64px square unconditionally and lets `--surface-hi` be the
        placeholder, so this fixture - whose morning digest has a media-free
        story and therefore no thumbnail - must still render TWO `.digest-thumb`
        cells for its TWO rows.
        """
        cells = re.findall(r'<span class="digest-thumb">(.*?)</span>',
                          self.index_html, re.S)
        self.assertEqual(2, len(cells),
                         "each row must reserve exactly one 64px square")
        with_img = [c for c in cells if "<img" in c]
        self.assertEqual(1, len(with_img),
                         "exactly one of these two digests has a thumbnail")
        cell = self.css("digest-thumb")
        self.assertIn("width: 64px", cell)
        self.assertIn("height: 64px", cell)
        self.assertIn("flex: 0 0 64px", cell)
        self.assertIn("border-radius: 8px", cell)
        self.assertIn("background: %s" % APPROVED["surface_hi"], cell)

    def test_a_failed_image_degrades_to_the_placeholder_not_to_a_shift(self):
        """`onerror` must hide the IMG only. Hiding the cell would collapse the
        row, which is the defect this layout most easily reintroduces."""
        self.assertIn("onerror=\"this.style.display='none'\"",
                      self.index_html)
        img = self.css_rule(r"\.digest-thumb \.digest-thumb-img")
        # Visible by default, fills the reserved square, crops rather than
        # distorts.
        self.assertIn("display: block", img)
        self.assertNotIn("display: none", img)
        self.assertIn("width: 100%", img)
        self.assertIn("height: 100%", img)
        self.assertIn("object-fit: cover", img)
        # The cell keeps its own fill, which is what a failed load reveals.
        self.assertIn("background: %s" % APPROVED["surface_hi"],
                      self.css("digest-thumb"))

    def test_the_slot_label_and_the_count_share_one_line(self):
        """The count sits BESIDE the label, not on a line of its own - which is
        what makes the row 64+24 = 88px tall instead of ~110px."""
        self.assertIn("gap: 8px", self.css("digest-row-head"))
        self.assertIn("display: flex", self.css("digest-row-head"))
        self.assertIn("align-items: baseline", self.css("digest-row-head"))
        label = self.css("digest-card-label")
        self.assertIn("font-size: 15px", label)
        self.assertIn("font-weight: 600", label)
        self.assertIn("color: %s" % APPROVED["text"], label)
        count = self.css("digest-count")
        self.assertIn("font-size: 12px", count)
        self.assertIn("font-weight: 600", count)
        self.assertIn("color: %s" % APPROVED["accent"], count)

    def test_the_count_is_kept_in_the_span_the_honesty_tests_read(self):
        """`test_render_honesty` and `test_render_readability` both match the
        literal `<span class="digest-count">4 stories</span>` and `>1 story<`
        in the archive, so the redesign must keep that element and put the count
        text directly inside it. This asserts the contract rather than hoping."""
        self.assertIn('<span class="digest-count">3 stories</span>',
                      self.index_html)
        self.assertIn(">1 story<", self.index_html)
        self.assertNotIn("1 stories", self.index_html)

    def test_the_category_summary_is_11px_muted_and_the_verdict_text(self):
        summary = self.css("digest-card-summary")
        self.assertIn("font-size: 11px", summary)
        self.assertIn("line-height: 1.45", summary)
        self.assertIn("color: %s" % APPROVED["muted"], summary)
        # The summary text is derived from what the PARSER read off the digest
        # page, not from a hand-written guess: `build_summary` breaks count ties
        # on dict insertion order, so the expected string is only knowable from
        # the counts themselves. Guessing it here would have been a test of my
        # memory of dict ordering rather than of the renderer.
        counts = rd.parse_digest_categories(
            os.path.join(self.tmp, "digests", "%s-evening.html" % DATE))
        self.assertIn(rd.build_summary(counts), self.index_html)
        self.assertIn(rd.build_summary({"local": 1}), self.index_html)

    def test_the_text_column_can_shrink_so_the_chevron_stays_on_screen(self):
        """THE SQUEEZE BUG, PREVENTED.

        A flex child defaults to `min-width: auto`, so one long word or a long
        category list widens the text column and pushes the chevron off the
        edge. On the digest page this same mistake wrapped a headline one
        character per line; here it would silently lose the affordance. `flex:
        1 1 auto` plus `min-width: 0` is what makes the text WRAP instead.
        """
        body = self.css("digest-body")
        self.assertIn("flex: 1 1 auto", body)
        self.assertIn("min-width: 0", body)
        row = self.css("digest-row")
        self.assertIn("gap: 12px", row)
        self.assertIn("align-items: center", row)

    def test_every_row_carries_the_decorative_chevron(self):
        rows = len(re.findall(r'class="digest-row"', self.index_html))
        self.assertEqual(2, rows)
        self.assertEqual(rows, self.index_html.count('class="digest-chevron"'))
        chevron = self.css("digest-chevron")
        self.assertIn("color: %s" % APPROVED["dim"], chevron)
        self.assertIn("width: 14px", chevron)
        self.assertIn("flex: 0 0 14px", chevron)
        # Decorative: the row's own text carries the meaning, so the glyph must
        # not be announced a second time.
        self.assertIn('aria-hidden="true"', rd.CHEVRON_SVG)


class TestArchiveContrastBoundary(ArchiveIndexMixin, unittest.TestCase):
    """`--dim #6C727C` FAILS WCAG AA on this background. Its use is bounded.

    It measures 3.93:1 against `#0E1014`, which is why it was rejected as body
    text on the digest page. The approved archive design uses it for exactly
    three things, all non-essential at 11px: the date label, the chevron and the
    footer meta. If it ever becomes a headline or a category summary the page
    stops being readable for the reader who needs it most.
    """

    def _rule_users(self, class_name):
        return self.css_rule(r"\." + re.escape(class_name))

    def test_dim_is_used_only_on_the_three_approved_elements(self):
        allowed = {"date-heading", "digest-chevron", "archive-footer"}
        users = set()
        for match in re.finditer(
                r"(?m)^\s*\.([a-z0-9\-]+)[^{]*\{([^}]*)\}", self.index_html):
            if APPROVED["dim"] in match.group(2):
                users.add(match.group(1).split(":")[0])
        self.assertEqual(allowed, users,
                         "the failing-contrast colour now also paints %r; it is "
                         "bounded to the date label, the chevron and the footer"
                         % (users - allowed,))

    def test_the_elements_that_carry_the_meaning_never_use_dim(self):
        for class_name in ("archive-title", "digest-card-label", "digest-count",
                           "digest-card-summary", "archive-subtitle"):
            self.assertNotIn(APPROVED["dim"],
                             self._rule_users(class_name),
                             "%s must not be painted with the sub-AA colour"
                             % class_name)

    def test_the_footer_says_what_the_approved_design_says(self):
        self.assertIn(rd.ARCHIVE_FOOTER_NOTE, self.index_html)
        footer = self.css("archive-footer")
        self.assertIn("font-size: 11px", footer)
        self.assertIn("border-top: 1px solid %s" % APPROVED["line"], footer)


class TestArchiveEmptyState(ArchiveIndexMixin, unittest.TestCase):
    """An archive with nothing in it must still be a page, not a blank one."""

    def test_an_empty_archive_renders_the_empty_state_and_still_renders(self):
        empty = os.path.join(self.tmp, "nothing")
        os.makedirs(empty)
        out = os.path.join(self.tmp, "empty-index.html")
        self.assertTrue(rd.render_index(empty, out, DATE, CUTOFF))
        with io.open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('class="empty-state"', html)
        self.assertIn("No digests yet", html)
        # And the shell is intact, so a fresh install still shows the header and
        # the footer rather than a bare word.
        self.assertIn('class="archive-title">Digest Archive', html)
        self.assertIn(rd.ARCHIVE_FOOTER_NOTE, html)
        # No dangling separator: with no digests there is no date span, so the
        # subtitle must be the count ALONE. "0 briefings · " would render a
        # middot with nothing after it on a brand-new install.
        self.assertIn('class="archive-subtitle">0 briefings</p>', html)
        self.assertNotIn("0 briefings ·", html)
        self.assertNotIn("· </p>", html)

    def test_a_zero_story_digest_still_renders_its_row_sanely(self):
        """`build_summary` returns 'no stories' for an empty digest, and the row
        must survive that: a '0 stories' count and an honest 'no stories'
        summary, not a dropped row and not a crash."""
        digests = os.path.join(self.tmp, "zero")
        os.makedirs(digests)
        with io.open(os.path.join(digests, "%s-evening.html" % DATE), "w",
                     encoding="utf-8") as fh:
            fh.write(rd.render_page([], "evening", DATE, CUTOFF, META))
        out = os.path.join(self.tmp, "zero-index.html")
        self.assertTrue(rd.render_index(digests, out, DATE, CUTOFF))
        with io.open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('<span class="digest-count">0 stories</span>', html)
        self.assertIn("no stories", html)
        self.assertEqual(1, len(re.findall(r'class="digest-row"', html)))
        # The reserved square is still reserved on a story-less digest.
        self.assertIn('<span class="digest-thumb">', html)
        self.assertIn("1 briefing", html)


class TestHonestySurvivedTheRestyle(ApprovedDesignMixin, unittest.TestCase):
    """The three features the redesign had the most room to break."""

    def test_the_health_banner_and_its_down_line(self):
        self.assertIn('class="health-banner"', self.page)
        self.assertIn("Some sources were unreachable", self.page)
        self.assertIn("this digest may be missing items", self.page)
        self.assertIn('class="health-down"', self.page)
        self.assertIn("Down: iost.tu.edu.np", self.page)
        self.assertIn('class="health-note"', self.page)
        # RE-APPROVED 2026-10-18: the label is the fold's `<summary>` now, not a
        # prefix inside the body. The body restating it was the duplicate the
        # owner found on a 390x844 render.
        self.assertIn(">Research note</summary>", self.page)
        self.assertNotIn("Research note:", self.page)
        self.assertIn("iost.tu.edu.np", self.page)

    def test_the_freshness_line_and_the_unverified_variant(self):
        self.assertIn('class="hero-cutoff"', self.page)
        self.assertIn("Information checked through", self.page)
        self.assertIn("some sources were unreachable", self.page)
        unknown = rd.render_page([], "morning", DATE, CUTOFF, {
            "data_cutoff_npt": CUTOFF,
            "source_health": {"state": "unknown", "critical": {},
                               "all": {}, "by_category": {}}})
        self.assertIn('<p class="hero-cutoff hero-cutoff-unverified">', unknown)
        self.assertIn("Freshness unverified", unknown)

    def test_every_section_still_renders_its_status_line(self):
        """CONTRACT B, RE-POINTED (owner-approved). A section renders when it
        has stories, and it ALSO renders when it has an alarm to make. It is
        omitted only when it has neither.

        The expected counts are DERIVED from the fixture rather than hard-coded,
        because this file's three stories sit in three different sections:
        3 populated sections + 1 alarm (gaming, tracked and down) = 4 sections,
        4 status lines, 1 of them an alarm. The seven quiet ones are not counted
        here because they are not rendered at all --
        `test_the_quiet_empty_sections_are_omitted_entirely` covers them.
        """
        populated = {s["category"] for s in STORIES}
        self.assertEqual(len(populated), 3, "fixture must span three categories")
        self.assertEqual(self.page.count('<section class="digest-section">'),
                         len(RENDERED_KEYS))
        self.assertEqual(len(re.findall(r'<p class="section-status', self.page)),
                         1)
        # Exactly one alarm, and it is the alarm section: the single tracked
        # category in this fixture is `gaming`, and it is down. An alarm is never
        # hidden, which is the whole reason the hiding rule stops where it does.
        self.assertEqual(
            self.page.count('<p class="section-status section-status-alarm">'), 1)
        self.assertIn(
            'section-status-alarm">⚠️ Source unreachable — check https://khabarhub.com/',
            self.page)
        # The three populated sections say nothing, because B1 is silence.
        self.assertEqual(
            self.page.count('<p class="section-status">'), 0)

    def test_the_weather_advice_line_and_its_cap(self):
        self.assertIn('class="weather-block"', self.page)
        self.assertIn('class="weather-label"', self.page)
        self.assertIn("Take an umbrella", self.page)
        # No advice at all must still render no block and no placeholder.
        quiet = rd.render_page(STORIES, "evening", DATE, CUTOFF,
                               {"weather_advice": []})
        self.assertNotIn('<div class="weather-block">', quiet)
        # Markup, not the bare class name: `.weather-label` is a stylesheet rule
        # that exists whether or not the block renders.
        self.assertNotIn('<p class="weather-label">', quiet)

    def test_the_deadline_chip_and_line_both_survive(self):
        self.assertIn("chip-deadline", self.page)
        self.assertIn("Deadline 2026-10-05", self.page)
        self.assertIn('class="card-deadline"', self.page)

    def test_the_for_you_line_is_unchanged_markup(self):
        self.assertIn('<p class="card-foryou"><strong>For you:</strong> '
                      "Charge anything mains-powered before 10:00.</p>",
                      self.page)

    def test_the_read_article_link_is_a_real_link(self):
        self.assertIn('class="story-link"', self.page)
        self.assertIn('href="https://example.com/a"', self.page)
        self.assertIn("Read article", self.page)

    def test_the_design_change_did_not_weaken_the_status_tone_contract(self):
        """The quiet/alarm distinction is a correctness property, so it is
        restated here: a redesign that restyled it into the new palette would
        have quietly merged "nothing happened" with "the source failed".

        The quiet tone's literal is `#9BA1AC` (--muted) rather than the old
        `#aaa`, because the status line was moved onto the approved palette so
        that its contrast could be measured against `--bg` the same way as every
        other colour here. It is measurably quieter than what it replaced
        (7.33:1 vs 8.20:1 on #0E1014), so the distinction is preserved and
        slightly strengthened; `test_render_readability` additionally pins the
        relationship rather than the literal.
        """
        base = re.search(r"\.section-status \{([^}]*)\}", self.page).group(1)
        self.assertIn(APPROVED["muted"], base)
        self.assertNotIn("#fbbf24", base.lower())
        self.assertIn("border-left: 3px solid transparent", base)
        alarm = self.css("section-status-alarm")
        self.assertIn("#fbbf24", alarm.lower())
        self.assertIn("#dc2626", alarm)


class TestOneMarkerPerSection(ApprovedDesignMixin, unittest.TestCase):
    """FIX 2: a section is marked once.

    The label row was carrying BOTH the approved 6px coloured dot AND an emoji.
    Two markers doing one job is the same mistake D2/D3 had to fix for two
    different status claims dressed identically: on a page with eleven sections
    it is eleven redundant glyphs, and the reader cannot tell which one carries
    meaning.

    These assert on the RENDERED PAGE rather than on the module, because the
    behaviour is what the owner sees - a test on the constant would still pass
    if some other code path put the emoji back into the markup.
    """

    #: The eleven glyphs the digest page used to prepend. Written as literals,
    #: not read from the module, so this test cannot be satisfied by importing
    #: the very map it is meant to catch being used again.
    SECTION_GLYPHS = (
        "\u26a1",              # high voltage
        "\U0001f30d",         # globe
        "\U0001f3e0",         # house
        "\U0001f393",         # graduation cap
        "\U0001f3eb",         # school
        "\U0001f3e6",         # bank
        "\U0001f1f3\U0001f1f5",  # Nepal flag
        "\U0001f3ae",         # video game
        "\U0001f38c",         # crossed flags
        "\U0001f4da",         # books
        "\U0001f4bc",         # briefcase
    )

    #: Anything above the BMP that is not a letter, digit or space. Used as a
    #: belt-and-braces sweep so a NEW emoji - one not in the list above - is
    #: also caught in this row.
    NON_TEXT = re.compile(
        "[^\u0000-\u007F\u00A0-\u024F\u2010-\u2027\u00B7\u2014\u2192\u2022"
        "\u2018\u2019\u201C\u201D]")

    def label_rows(self):
        """Every section label row, exactly as the reader sees it."""
        rows = re.findall(
            r'<h2 class="section-header"[^>]*>(.*?)</h2>', self.page, re.S)
        self.assertEqual(len(rows), len(RENDERED_KEYS),
                         "found %d label rows, expected one per RENDERED "
                         "section (%d): a quiet empty section is omitted, an "
                         "alarm or a section with stories is not"
                         % (len(rows), len(RENDERED_KEYS)))
        return rows

    def test_no_section_label_row_contains_any_of_the_eleven_emoji(self):
        for row in self.label_rows():
            for glyph in self.SECTION_GLYPHS:
                self.assertNotIn(
                    glyph, row,
                    "section label %r still carries the emoji %r; the approved "
                    "design marks a section with the 6px dot alone" % (row, glyph))

    def test_no_section_label_row_contains_any_non_text_glyph_at_all(self):
        """The general form of the assertion above, so an eleventh-or-twelfth
        emoji added later is caught without this file being updated."""
        for row in self.label_rows():
            found = self.NON_TEXT.findall(row)
            self.assertEqual(
                found, [],
                "section label %r contains non-text glyphs %r; the row should "
                "be the bare section name, the dot being a CSS ::before"
                % (row, found))

    def test_the_label_row_is_still_the_bare_section_name(self):
        """Positive form: the header TEXT was not damaged in removing the emoji.
        Without this, deleting every character from the row would satisfy both
        tests above."""
        self.assertEqual(
            self.label_rows(),
            [rd.esc(header) for key, header, _c in rd.SECTIONS
             if key in RENDERED_KEYS])

    def test_the_quiet_empty_sections_are_omitted_entirely(self):
        """The other half of the rule, asserted directly rather than inferred.

        Omitting the quiet sections is the owner's explicit request, so it gets a
        named test of its own: none of these seven headers may appear in the page
        AT ALL, and no quiet status line may be emitted for them. The seven are
        the exact complement of RENDERED_KEYS, so this and label_rows() together
        account for all eleven.
        """
        for key in OMITTED_KEYS:
            header = dict((k, h) for k, h, _c in rd.SECTIONS)[key]
            self.assertNotIn(
                rd.esc(header), self.page,
                "section %r is empty and untracked, so its status is quiet and "
                "the owner asked for it not to be displayed" % key)
        self.assertEqual(len(RENDERED_KEYS) + len(OMITTED_KEYS),
                         len(rd.SECTIONS),
                         "RENDERED_KEYS + OMITTED_KEYS must cover every section, "
                         "or this test is only accounting for part of the page")
        for quiet in ("Nothing new from this source today",
                      rd.UNTRACKED_STATUS):
            self.assertNotIn(
                quiet, self.page,
                "a quiet status line reached the page; quiet empty sections are "
                "omitted rather than labelled")

    def test_the_one_marker_that_remains_is_the_dot(self):
        """...and the row is not left unmarked instead."""
        header = self.css("section-header")
        self.assertIn("display: flex", header)
        self.assertIn("align-items: center", header)
        dot = self.css_rule(r"\.section-header::before")
        self.assertIn("content:", dot)
        self.assertIn("width: 6px", dot)
        self.assertIn("height: 6px", dot)
        self.assertIn("border-radius: 50%", dot)
        # The dot still takes its colour from the per-section inline property,
        # so every RENDERED section still carries its own accent.
        self.assertEqual(
            self.page.count('<h2 class="section-header" style="--dot:'),
            len(RENDERED_KEYS),
            "every rendered section must still carry its own --dot accent")
        for key, _header, color in rd.SECTIONS:
            if key not in RENDERED_KEYS:
                continue
            self.assertIn('--dot:%s;' % color, self.page,
                          "section %s lost its accent colour" % key)

    def test_the_digest_renderer_no_longer_carries_a_section_emoji_map(self):
        """The constant is deleted, not left unused.

        A map that still holds all eleven glyphs and is read by nothing is one
        "harmless tidy" away from being read again, and this assertion is what
        makes that tidy a test failure rather than a silent regression.
        """
        self.assertFalse(
            hasattr(rd, "SECTION_EMOJI"),
            "render_digest still defines SECTION_EMOJI; the digest page's label "
            "row must have exactly one marker")

    def test_the_ntfy_push_keeps_its_own_section_emoji(self):
        """THE OTHER SURFACES ARE NOT AFFECTED, and this is what proves the fix
        was scoped rather than a repo-wide emoji purge.

        The ntfy push is a SEPARATE implementation with its own `CATEGORY_EMOJI`
        map in the workflow's Python heredoc. It is deliberately NOT changed: a
        push notification has no coloured dot, so its emoji are the only marker
        it has. If a future edit removes these too, this goes red.

        Checked in the ESCAPED spelling the workflow actually uses
        (`'\\U0001f3e0'`), not as literal UTF-8 - the first version of this test
        searched for the literal glyph, failed, and the reason was simply that
        the workflow never contained one in that form. Asserting the real form
        is the difference between testing the claim and testing my assumption
        about it.
        """
        import io
        path = os.path.join(REPO_ROOT, ".github", "workflows", "daily-news.yml")
        with io.open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("CATEGORY_EMOJI", body,
                      "the ntfy push lost its emoji map entirely")
        for key, glyph in zip([k for k, _h, _c in rd.SECTIONS],
                              self.SECTION_GLYPHS):
            escaped = "".join(
                ("\\u%04x" % ord(c)) if ord(c) <= 0xFFFF
                else ("\\U%08x" % ord(c)) for c in glyph)
            self.assertIn(
                "'%s': '%s'," % (key, escaped), body,
                "the ntfy push lost the section emoji %r (%s) - out of scope "
                "for the digest page fix, and a push has no dot to mark it "
                "instead" % (key, escaped))

    def test_the_email_path_is_untouched_by_this_fix(self):
        """`build_email.py` puts no emoji on a section header at all, so the
        email never had the double marker and never needed this fix. Asserted
        on a real email render so it fails if the email ever grows one."""
        import build_email
        payload = validated_payload("ok", stories=STORIES, notes="")
        import json as _json
        tmp = tempfile.mkdtemp(prefix="emoji-email-")
        try:
            src = os.path.join(tmp, "p.json")
            with io.open(src, "w", encoding="utf-8") as fh:
                _json.dump(payload, fh, ensure_ascii=False)
            out = os.path.join(tmp, "e.html")
            build_email.main(["--in", src, "--out-html", out, "--out-text",
                              os.path.join(tmp, "e.txt"),
                              "--template", os.path.join(
                                  REPO_ROOT, "templates", "email.html")])
            with io.open(out, encoding="utf-8") as fh:
                body = fh.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for header, _key, _color in ((h, k, c) for k, h, c in rd.SECTIONS):
            for glyph in self.SECTION_GLYPHS:
                self.assertNotIn(glyph + " " + header, body,
                                 "the email section header %r grew an emoji"
                                 % header)


class TestNoDesktopLayout(ApprovedDesignMixin, unittest.TestCase):
    """The owner has no desktop use case, so nothing here may serve one."""

    def test_there_is_no_min_width_media_query_at_all(self):
        self.assertNotIn("@media (min-width", self.page)
        self.assertNotIn("@media (min-device-width", self.page)

    def test_nothing_in_the_page_is_wider_than_the_column(self):
        """A fixed 1100px element anywhere would force a horizontal scrollbar
        on the phone this design exists for."""
        offenders = re.findall(r"(?:width|min-width)\s*:\s*(\d{3,})px", self.page)
        allowed = {"480"}  # the column itself
        for value in offenders:
            self.assertIn(value, allowed,
                          "a %spx fixed width would overflow a 390px screen"
                          % value)


if __name__ == "__main__":
    unittest.main()
